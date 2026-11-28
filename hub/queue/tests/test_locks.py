"""The queue lock is never held during a call to Home Assistant or the bridge."""
import threading
import time

import pytest

from queue_engine import Queue, QueueError

F = "a.gcode.3mf"


def run(fn, *a, **kw):
    out = {}

    def target():
        try:
            out["value"] = fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            out["error"] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, out


def within(seconds, fn, *a):
    t, out = run(fn, *a)
    t.join(seconds)
    return not t.is_alive(), out


def test_snapshot_returns_while_send_print_blocks(make, fake):
    inside, release = threading.Event(), threading.Event()

    def slow_send(data):
        inside.set()
        release.wait(10)
    make.send_print = slow_send
    job = make.add(F, target="S1")
    t, out = run(make.start, "S1", plate_clear=True)
    assert inside.wait(5)
    ok, snap = within(0.5, make.snapshot)
    state = make.jobs[0]["state"]
    release.set()
    t.join(5)
    assert ok, "snapshot waited for the send"
    assert state == "sending" and len(snap["value"]["jobs"]) == 1
    assert out["value"]["state"] == "starting" and make.jobs[0]["id"] == job["id"]


def test_other_calls_see_a_sending_printer_as_busy(make, fake):
    inside, release = threading.Event(), threading.Event()
    make.send_print = lambda data: (inside.set(), release.wait(10))
    make.add(F, target="S1")
    t, _ = run(make.start, "S1", plate_clear=True)
    assert inside.wait(5)
    with pytest.raises(QueueError, match="already has a job"):
        make.start("S1", plate_clear=True)
    release.set()
    t.join(5)


def test_four_parallel_starts_send_one_print(make, fake):
    make.add(F, target="S1")
    make.add("b.gcode.3mf", target="S1")
    sent = []

    def send(data):
        sent.append(data["file"])
        time.sleep(0.2)
    make.send_print = send
    runs = [run(make.start, "S1", plate_clear=True) for _ in range(4)]
    for t, _ in runs:
        t.join(10)
    assert sent == [F]
    assert sum("value" in o for _, o in runs) == 1
    assert sum(isinstance(o.get("error"), QueueError) for _, o in runs) == 3


def test_failed_send_puts_the_job_back_first(make, fake):
    job = make.add(F, target="S1")
    fake.fail[("druckwerk", "print_file")] = "G-code guard: no filament"
    with pytest.raises(QueueError, match="G-code guard"):
        make.start("S1", plate_clear=True)
    j = make.jobs[0]
    assert (j["id"], j["state"], j.get("printer"), "started_at" in j) == (job["id"], "queued", None, False)
    fake.fail.clear()
    assert make.start("S1", plate_clear=True)["state"] == "starting"


def test_a_job_that_was_sending_at_a_restart_is_failed(make, fake, tmp_path):
    inside = threading.Event()
    make.send_print = lambda data: (inside.set(), time.sleep(10))
    make.add(F, target="S1")
    run(make.start, "S1", plate_clear=True)
    assert inside.wait(5)
    again = Queue(make.registry, str(tmp_path), fake.get_state, fake.call_service)
    assert not again.jobs
    assert again.history[-1]["state"] == "failed" and "check the printer" in again.history[-1]["error"]


def test_tick_reads_the_status_once_and_not_under_the_lock(make, fake):
    make.add(F, target="S1")
    make.start("S1", plate_clear=True)
    reads, inside, release = [], threading.Event(), threading.Event()
    real = fake.get_state

    def get_state(eid):
        reads.append(eid)
        inside.set()
        release.wait(10)
        return real(eid)
    make.get_state = get_state
    t, _ = run(make.tick, "S1")
    assert inside.wait(5)
    ok, _ = within(0.5, make.snapshot)
    release.set()
    t.join(5)
    assert ok, "tick held the lock while it read the status"
    assert reads.count("sensor.one_print_status") == 1
