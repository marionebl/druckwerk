"""AMS drying as a queue job kind: it uses the same queue, busy checks and start path as a print."""
import pytest

from queue_engine import Queue, QueueError

F = "a.gcode.3mf"
DRY = {"ams_id": 0, "temp": 55, "hours": 8, "filament": "PLA"}


class FakeDry:
    """What the bridge does for the queue: start, stop and report drying per AMS unit."""

    def __init__(self):
        self.started, self.stopped, self.active, self.fail = [], [], {}, None

    def start(self, serial, d):
        if self.fail:
            raise RuntimeError(self.fail)
        self.started.append((serial, d))
        self.active[(serial, d["ams_id"])] = True

    def stop(self, serial, ams_id):
        self.stopped.append((serial, ams_id))
        self.active[(serial, ams_id)] = False

    def state(self, serial, ams_id):
        return {"active": self.active.get((serial, ams_id), False), "left_min": 60}


@pytest.fixture
def dry():
    return FakeDry()


@pytest.fixture
def q(make, dry, fake, tmp_path):
    make.dry = dry
    return make


def test_dry_job_is_a_queue_job(q):
    job = q.add_dry("S1", **DRY)
    assert (job["kind"], job["state"], job["target"], job["file"]) == ("dry", "queued", "S1", "")
    assert job["dry"] == {"ams_id": 0, "temp": 55, "hours": 8, "filament": "PLA"}
    assert q.jobs == [job]


@pytest.mark.parametrize("bad", [{"temp": 30}, {"temp": 95}, {"temp": "55"}, {"hours": 0}, {"hours": 73},
                                 {"hours": 1.5}, {"ams_id": -1}, {"ams_id": 3.0}, {"ams_id": 100}, {"filament": 5}])
def test_dry_job_validation(q, bad):
    with pytest.raises(ValueError):
        q.add_dry("S1", **dict(DRY, **bad))
    assert q.jobs == []


def test_dry_job_needs_a_known_printer(q):
    with pytest.raises(ValueError, match="unknown printer"):
        q.add_dry("NOPE", **DRY)


def test_start_needs_confirmation_but_no_plate_clear(q, dry):
    job = q.add_dry("S1", **DRY)
    with pytest.raises(QueueError, match="confirm"):
        q.start("S1", job_id=job["id"])
    assert dry.started == []
    started = q.start("S1", confirmed=True, job_id=job["id"])
    assert (started["state"], started["printer"]) == ("drying", "S1")
    assert dry.started == [("S1", {"ams_id": 0, "temp": 55, "hours": 8, "filament": "PLA"})]


def test_print_start_still_needs_plate_clear(q):
    q.add(F, target="S1")
    with pytest.raises(QueueError, match="plate"):
        q.start("S1", confirmed=True)


def test_dry_job_waits_behind_a_print(q, fake, dry):
    p = q.add(F, target="S1")
    d = q.add_dry("S1", **DRY)
    assert q.next_for("S1")["id"] == p["id"]  # queue order: the print comes first
    q.start("S1", plate_clear=True)
    assert q.jobs[0]["state"] == "starting"
    with pytest.raises(QueueError, match="already has a job"):
        q.start("S1", confirmed=True, job_id=d["id"])  # the printer is busy with the print
    assert dry.started == []
    q.jobs[0]["state"] = "printing"
    fake.states["sensor.one_print_status"] = "finish"
    q.tick("S1")  # the print is done
    assert q.next_for("S1")["id"] == d["id"]
    q.start("S1", confirmed=True)
    assert d["state"] == "drying" and dry.started


def test_start_next_runs_a_dry_job_when_it_is_first(q, dry):
    d = q.add_dry("S1", **DRY)
    q.add(F, target="S1")
    assert q.start("S1", confirmed=True)["id"] == d["id"]


def test_print_waits_behind_a_running_dry_job(q, fake, dry):
    q.start("S1", confirmed=True, job_id=q.add_dry("S1", **DRY)["id"])
    q.add(F, target="S1")
    with pytest.raises(QueueError, match="already has a job"):
        q.start("S1", plate_clear=True)
    assert not [c for c in fake.calls if c[1] == "print_file"]
    # Another printer is not blocked.
    q.add("b.gcode.3mf", target="S3")
    assert q.start("S3", plate_clear=True)["printer"] == "S3"


def test_dry_job_respects_the_printer_status(q, fake, dry):
    fake.states["sensor.one_print_status"] = "running"
    d = q.add_dry("S1", **DRY)
    with pytest.raises(QueueError, match="not ready"):
        q.start("S1", confirmed=True, job_id=d["id"])
    assert dry.started == [] and d["state"] == "queued"


def test_failed_dry_start_keeps_the_job_with_the_error(q, dry):
    d = q.add_dry("S1", **DRY)
    dry.fail = "AMS busy"
    with pytest.raises(QueueError, match="AMS busy"):
        q.start("S1", confirmed=True, job_id=d["id"])
    assert (d["state"], d["error"]) == ("queued", "AMS busy")


def test_cancel_a_waiting_dry_job(q, dry):
    d = q.add_dry("S1", **DRY)
    q.cancel(d["id"])
    assert q.jobs == [] and q.history[-1]["state"] == "cancelled"
    assert dry.started == [] and dry.stopped == []


def test_stop_a_running_dry_job(q, dry):
    d = q.add_dry("S1", **DRY)
    q.start("S1", confirmed=True, job_id=d["id"])
    q.stop_dry(d["id"])
    assert dry.stopped == [("S1", 0)]
    assert q.jobs == [] and (q.history[-1]["state"], q.history[-1]["error"]) == ("cancelled", "stopped")
    q.add(F, target="S1")
    assert q.start("S1", plate_clear=True)["state"] == "starting"  # the printer is free again


def test_stop_refuses_a_job_that_is_not_drying(q):
    d = q.add_dry("S1", **DRY)
    with pytest.raises(QueueError, match="not drying"):
        q.stop_dry(d["id"])
    p = q.add(F, target="S1")
    with pytest.raises(QueueError, match="not drying"):
        q.stop_dry(p["id"])


def test_stop_keeps_the_job_when_the_bridge_fails(q, dry):
    d = q.add_dry("S1", **DRY)
    q.start("S1", confirmed=True, job_id=d["id"])
    dry.stop = lambda serial, ams: (_ for _ in ()).throw(RuntimeError("bridge down"))
    with pytest.raises(QueueError, match="bridge down"):
        q.stop_dry(d["id"])
    assert d["state"] == "drying"


def test_cancel_of_a_running_dry_job_says_stop(q):
    d = q.add_dry("S1", **DRY)
    q.start("S1", confirmed=True, job_id=d["id"])
    with pytest.raises(QueueError, match="stop"):
        q.cancel(d["id"])


def test_tick_finishes_the_job_when_the_ams_stopped_drying(q, fake, dry):
    d = q.add_dry("S1", **DRY)
    q.start("S1", confirmed=True, job_id=d["id"])
    fake.now += 30
    q.tick("S1")
    assert d["state"] == "drying"  # inside the grace period the AMS may not report yet
    fake.now += 600
    q.tick("S1")
    assert d["state"] == "drying"  # still active
    dry.active[("S1", 0)] = False
    q.tick("S1")
    assert d["state"] == "done" and q.history[-1] is d


def test_tick_falls_back_to_the_time_when_the_state_is_unknown(q, fake, dry):
    dry.state = lambda serial, ams_id: None
    d = q.add_dry("S1", **dict(DRY, hours=2))
    q.start("S1", confirmed=True, job_id=d["id"])
    fake.now += 2 * 3600
    q.tick("S1")
    assert d["state"] == "drying"
    fake.now += 11 * 60
    q.tick("S1")
    assert d["state"] == "done"


def test_dry_jobs_reorder_and_move_like_prints(q):
    d = q.add_dry("S1", **DRY)
    p = q.add(F, target="S1")
    q.reorder(p["id"], 0)
    assert [j["id"] for j in q.jobs] == [p["id"], d["id"]]
    with pytest.raises(ValueError):
        q.move(d["id"], "any", "H2S")  # a dry job belongs to one printer


def test_dry_job_survives_a_restart(q, tmp_path, fake, dry):
    q.add_dry("S1", **DRY)
    again = Queue(q.registry, str(tmp_path), fake.get_state, fake.call_service, clock=lambda: fake.now)
    assert again.jobs[0]["kind"] == "dry" and again.jobs[0]["dry"]["temp"] == 55


def test_pending_approval_dry_job_is_not_startable(q):
    d = q.add_dry("S1", pending=True, **DRY)
    assert d["state"] == "pending_approval" and q.next_for("S1") is None
    q.approve(d["id"])
    assert q.next_for("S1")["id"] == d["id"]
