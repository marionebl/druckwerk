import pytest

from queue_engine import QueueError

F = "a.gcode.3mf"


def ids(q):
    return [j["id"] for j in q.jobs]


def test_reorder(make):
    a, b, c = (make.add(n + ".gcode.3mf", target="S1") for n in "abc")
    make.reorder(c["id"], 0)
    assert ids(make) == [c["id"], a["id"], b["id"]]
    make.reorder(c["id"], 99)
    assert ids(make) == [a["id"], b["id"], c["id"]]


def test_any_matches_model(make):
    job = make.add(F, target="any", model="P1S")
    assert make.next_for("S1") is None
    assert make.next_for("S2")["id"] == job["id"]


def test_any_matches_filament_type_and_colour(make, fake):
    make.add(F, target="any", model="H2S", filaments=[{"type": "PETG", "colour": "#FF0000"}])
    fake.trays["S1"] = [{"type": "PETG", "colour": "#00FF00"}]
    fake.trays["S3"] = [{"type": "PETG", "colour": "#ff0000"}]
    assert make.next_for("S1") is None
    assert make.next_for("S3") is not None


def test_refuses_when_running(make, fake):
    make.add(F, target="S1")
    fake.states["sensor.one_print_status"] = "running"
    with pytest.raises(QueueError):
        make.start("S1", plate_clear=True)
    assert not [c for c in fake.calls if c[1] == "print_file"]


def test_requires_plate_clear(make):
    make.add(F, target="S1")
    with pytest.raises(QueueError):
        make.start("S1")
    with pytest.raises(QueueError):
        make.start("S1", plate_clear="yes")


def test_guard_refusal_keeps_job_first_with_error(make, fake):
    first = make.add(F, target="S1")
    make.add("b.gcode.3mf", target="S1")
    fake.fail[("druckwerk", "print_file")] = "G-code guard: no filament"
    with pytest.raises(QueueError, match="G-code guard"):
        make.start("S1", plate_clear=True)
    assert make.jobs[0]["id"] == first["id"]
    assert make.jobs[0]["state"] == "queued"
    assert "G-code guard" in make.jobs[0]["error"]


def test_pause_entities_reenabled_after_failed_send(make, fake):
    make.add(F, target="S1")
    fake.fail[("druckwerk", "print_file")] = "boom"
    with pytest.raises(QueueError):
        make.start("S1", plate_clear=True)
    assert fake.states["automation.snap"] == "on"
    assert [c[1] for c in fake.calls] == ["turn_off", "print_file", "turn_on"]


def test_send_uses_force_and_options(make, fake):
    make.add(F, target="S1", options={"timelapse": True})
    make.start("S1", plate_clear=True)
    data = [c for c in fake.calls if c[1] == "print_file"][0][2]
    assert data == {"serial": "S1", "file": F, "timelapse": True, "bed_leveling": True,
                    "use_ams": True, "force": True}


def start_job(make):
    make.add(F, target="S1")
    return make.start("S1", plate_clear=True)


def test_running_with_idle_stage_and_no_tray_is_not_started(make, fake):
    job = start_job(make)
    fake.states.update({"sensor.one_print_status": "running", "sensor.one_current_stage": "idle",
                        "sensor.one_active_tray": "none", "sensor.one_print_progress": "5"})
    make.tick("S1")
    assert job["state"] == "starting"


def test_changing_filament_with_tray_zero_starts(make, fake):
    job = start_job(make)
    fake.states.update({"sensor.one_current_stage": "changing_filament", "sensor.one_active_tray": "0"})
    make.tick("S1")
    assert job["state"] == "printing"


def test_printing_with_progress_and_tray_starts(make, fake):
    job = start_job(make)
    fake.states.update({"sensor.one_current_stage": "printing", "sensor.one_active_tray": "1",
                        "sensor.one_print_progress": "0"})
    make.tick("S1")
    assert job["state"] == "starting"
    fake.states["sensor.one_print_progress"] = "2"
    make.tick("S1")
    assert job["state"] == "printing"


def test_short_filament_change_seen_via_observe(make, fake):
    job = start_job(make)
    make.observe("S1", "changing_filament")
    fake.states.update({"sensor.one_current_stage": "printing", "sensor.one_active_tray": "2",
                        "sensor.one_print_progress": "0"})
    make.tick("S1")
    assert job["state"] == "printing"


def test_timeout_fails_with_reason(make, fake):
    job = start_job(make)
    fake.states.update({"sensor.one_print_status": "running", "sensor.one_current_stage": "idle",
                        "sensor.one_active_tray": "none"})
    fake.now += 599
    make.tick("S1")
    assert job["state"] == "starting"
    fake.now += 2
    make.tick("S1")
    assert job["state"] == "failed"
    assert "stage idle" in job["error"] and "tray none" in job["error"]
    assert make.history[-1]["id"] == job["id"]


def test_done_and_retry(make, fake):
    job = start_job(make)
    fake.states.update({"sensor.one_current_stage": "changing_filament", "sensor.one_active_tray": "0"})
    make.tick("S1")
    fake.states["sensor.one_print_status"] = "finish"
    make.tick("S1")
    assert job["state"] == "done"
    with pytest.raises(QueueError):
        make.retry(job["id"])


def test_cancel_and_retry(make):
    job = make.add(F, target="S1")
    make.cancel(job["id"])
    assert not make.jobs and make.history[-1]["state"] == "cancelled"
    assert make.retry(job["id"])["state"] == "queued"


def test_history_capped_and_persisted(make, tmp_path, fake):
    for _ in range(205):
        make.cancel(make.add(F, target="S1")["id"])
    assert len(make.history) == 200
    from queue_engine import Queue
    again = Queue(make.registry, str(tmp_path), fake.get_state, fake.call_service)
    assert len(again.history) == 200


def test_print_options_are_validated_and_defaults_kept(make):
    job = make.add(F, target="S1")
    assert job["options"] == {"timelapse": False, "bed_leveling": True, "use_ams": True, "flow_cali": True,
                              "vibration_cali": True, "layer_inspect": False, "nozzle_offset_cali": False,
                              "bed_type": "auto", "ext_change_assist": False}
    job = make.add(F, target="S1", options={"bed_leveling": "auto", "bed_type": "cool_plate", "layer_inspect": True})
    assert (job["options"]["bed_leveling"], job["options"]["bed_type"], job["options"]["layer_inspect"]) == ("auto", "cool_plate", True)
    for bad in ({"bed_leveling": "maybe"}, {"vibration_cali": "auto"}, {"bed_type": "gold"}, {"layer_inspect": 1},
                {"nope": True}, {"nozzle_offset_cali": None}):
        with pytest.raises(ValueError, match="bad option"):
            make.add(F, target="S1", options=bad)


def test_send_passes_only_advanced_options_that_differ_from_the_default(make, fake):
    make.add(F, target="S1", options={"flow_cali": "auto", "bed_type": "hot_plate", "bed_leveling": "auto"})
    make.start("S1", plate_clear=True)
    data = [c for c in fake.calls if c[1] == "print_file"][0][2]
    assert data == {"serial": "S1", "file": F, "timelapse": False, "bed_leveling": "auto", "use_ams": True,
                    "flow_cali": "auto", "bed_type": "hot_plate", "force": True}


def test_job_name_defaults_to_the_file_and_is_limited(make):
    assert make.add("cube_x.gcode.3mf", target="S1")["name"] == "cube_x"
    assert make.add(F, target="S1", name="  Benchy for Anna ")["name"] == "Benchy for Anna"
    assert make.add(F, target="S1", name="")["name"] == "a"
    assert len(make.add(F, target="S1", name="x" * 80)["name"]) == 80
    with pytest.raises(ValueError, match="name"):
        make.add(F, target="S1", name="x" * 81)
    with pytest.raises(ValueError, match="name"):
        make.add(F, target="S1", name=5)


def test_a_failed_turn_on_of_a_paused_entity_is_logged_and_named_in_the_error(make, fake, caplog):
    make.add(F, target="S1")
    fake.fail[("homeassistant", "turn_on")] = "HA says no"
    fake.fail[("druckwerk", "print_file")] = "boom"
    with pytest.raises(QueueError, match=r"boom.*automation\.snap"):
        make.start("S1", plate_clear=True)
    assert "automation.snap" in make.jobs[0]["error"]
    records = [r for r in caplog.records if "automation.snap" in r.getMessage()]
    assert len(records) == 1 and "HA says no" in records[0].getMessage()


def test_a_failed_turn_on_after_a_sent_file_is_logged_and_the_job_still_starts(make, fake, caplog):
    make.add(F, target="S1")
    fake.fail[("homeassistant", "turn_on")] = "HA says no"
    job = make.start("S1", plate_clear=True)
    assert job["state"] == "starting" and job["error"] is None
    assert len([r for r in caplog.records if "automation.snap" in r.getMessage()]) == 1
