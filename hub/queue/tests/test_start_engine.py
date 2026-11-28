"""Start safety in the engine: an unsure send fails the job, a refusal keeps it; a start reads a fresh status; force-cancel."""
import pytest

from queue_engine import Queue, QueueError

F = "a.gcode.3mf"
LOST = "send timed out; check the printer before you retry"


class Unsure(Exception):
    unsure = True  # as the sender marks a send whose file may have reached the printer


def test_an_unsure_send_fails_the_job_and_sends_nothing_twice(make, fake):
    fake.fail[("druckwerk", "print_file")] = "timed out"
    make.send_print = lambda data: (_ for _ in ()).throw(Unsure("timed out"))
    make.add(F, target="S1")
    with pytest.raises(QueueError, match="send timed out"):
        make.start("S1", plate_clear=True)
    assert make.jobs == []
    (job,) = make.history
    assert (job["state"], job["error"]) == ("failed", LOST)
    with pytest.raises(QueueError):
        make.start("S1", plate_clear=True)


def test_a_refused_send_keeps_the_job_first(make, fake):
    fake.fail[("druckwerk", "print_file")] = "model mismatch"
    make.add(F, target="S1")
    with pytest.raises(QueueError, match="model mismatch"):
        make.start("S1", plate_clear=True)
    assert [j["state"] for j in make.jobs] == ["queued"] and make.history == []


def test_a_start_reads_the_status_through_the_fresh_reader(make, fake):
    seen = []

    def fresh(eid):
        seen.append(eid)
        return None  # older than the start limit
    make.get_start_state = fresh
    make.add(F, target="S1")
    with pytest.raises(QueueError, match="older than 60 s"):
        make.start("S1", plate_clear=True)
    assert seen == ["sensor.one_print_status"] and not [c for c in fake.calls if c[0] == "druckwerk"]


def test_force_cancel_ends_a_printing_job_as_failed(make, fake):
    job = make.add(F, target="S1")
    make.start("S1", plate_clear=True)
    job["state"] = "printing"
    with pytest.raises(QueueError, match="stop it there"):
        make.cancel(job["id"])
    done = make.cancel(job["id"], force=True)
    assert (done["state"], done["error"]) == ("failed", "removed by admin")
    assert make.jobs == [] and make.history[-1]["id"] == job["id"]


def test_force_cancel_does_not_touch_a_job_that_is_drying(make):
    make.dry = object()
    job = make.add_dry("S1", 0, 55, 4)
    job["state"], job["printer"] = "drying", "S1"
    with pytest.raises(QueueError, match="stop it first"):
        make.cancel(job["id"], force=True)
