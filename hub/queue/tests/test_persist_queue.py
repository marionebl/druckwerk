"""A tick rewrites queue.json only when a job changed."""
import queue_engine


def test_a_tick_writes_only_when_the_job_changed(make, fake, monkeypatch):
    make.add("a.gcode.3mf", target="S1")
    make.jobs[0].update(state="printing", printer="S1", started_at=fake.now)
    fake.states["sensor.one_print_status"] = "running"
    writes = []
    monkeypatch.setattr(queue_engine, "write_json", lambda *a: writes.append(1))
    for _ in range(30):
        make.tick("S1")
    assert writes == []
    fake.states["sensor.one_print_status"] = "finish"
    make.tick("S1")
    assert len(writes) == 1 and make.history[-1]["state"] == "done"
