"""The app sends a print itself: busy check, G-code guard, bridge call, per-printer result (action print_file)."""
import json
import os

import pytest

from hubfakes import GCODE, SERIAL, FakeBridge, make_3mf


@pytest.fixture
def hub(hub):
    hub.setup_printers()
    hub.seed_share()
    return hub


def put(hub, name="benchy.gcode.3mf", gcode=GCODE):
    return make_3mf(hub.media / "druckwerk" / name, gcode=gcode)


def run(hub, **data):
    return hub.req("POST", "/api/actions/print_file", dict({"serial": SERIAL, "file": "benchy.gcode.3mf"}, **data), secret=True)


def prints():
    return [b for path, b in FakeBridge.log if path == "/api/print"]


def test_happy_path_sends_the_file_and_records_it(hub):
    path = put(hub)
    status, body = run(hub, timelapse=True, flow_cali="auto")
    assert status == 200 and body == {"result": {"result": "sent"}}
    (sent,) = prints()
    assert sent["serial"] == SERIAL and sent["filename"] == "benchy.gcode.3mf" and sent["timelapse"] is True
    assert sent["use_ams"] is True and sent["flow_cali"] == "auto" and "force" not in sent and "layer_inspect" not in sent
    assert sent["bed_leveling"] is True  # the action's default "on" is true for the bridge
    r = hub.app.sender.result(SERIAL)
    assert (r["result"], r["last_file"], r["last_path"], r["last_error"]) == ("sent", "benchy.gcode.3mf", path, None)
    assert r["last_time"].startswith("1970-01-01T00:16:40")  # the fake clock
    with open(os.path.join(hub.tmp, "data", "sender.json")) as f:  # a restart keeps it
        assert json.load(f)[SERIAL]["last_path"] == path


def test_device_id_is_taken_as_the_serial_and_one_of_them_is_needed(hub):
    put(hub)
    assert hub.req("POST", "/api/actions/print_file", {"device_id": SERIAL, "file": "benchy.gcode.3mf"}, secret=True)[0] == 200
    status, body = hub.req("POST", "/api/actions/print_file", {"file": "benchy.gcode.3mf"}, secret=True)
    assert status == 400 and body["error"] == "must contain at least one of device_id, serial."


@pytest.mark.parametrize("file,text", [
    ("/etc/passwd", "Path /etc/passwd is outside the allowed folders (/media, /share)"),
    ("../x.gcode.3mf", "Path ../x.gcode.3mf is outside the druckwerk media folder"),
    ("benchy.stl", "Only .gcode.3mf files can be printed"),
    ("gone.gcode.3mf", "File not found: gone.gcode.3mf"),
])
def test_a_bad_path_is_a_400_and_sends_nothing(hub, file, text):
    put(hub)
    status, body = run(hub, file=file)
    assert (status, body["error"]) == (400, text) and prints() == []


def test_a_busy_printer_is_refused_and_force_overrides_it(hub):
    put(hub)
    for busy in ("RUNNING", "PAUSE"):
        FakeBridge.printer_extra = {SERIAL: {"print_status": busy}}
        status, body = run(hub)
        assert status == 409 and body["error"] == "Printer %s is %s; refusing to send a new print" % (SERIAL, busy)
    assert prints() == []
    assert run(hub, force=True)[0] == 200 and len(prints()) == 1
    FakeBridge.printer_extra = {SERIAL: {"print_status": "FINISH"}}
    assert run(hub)[0] == 200 and len(prints()) == 2


def test_the_guard_refuses_a_file_that_would_print_air_even_with_force(hub):
    put(hub, gcode=GCODE.replace("M620 S0A\nT0\nM621 S0A\n", ""))  # no AMS load
    for force in (False, True):
        status, body = run(hub, force=force)
        assert status == 409 and body["error"].startswith("Refusing to send benchy.gcode.3mf: No AMS filament load")
    assert prints() == []
    r = hub.app.sender.result(SERIAL)
    assert r["result"] == "error" and r["last_error"].startswith("Refusing to send") and r["last_path"] is None


def test_use_ams_false_skips_only_the_ams_load_check(hub):
    put(hub, gcode=GCODE.replace("M620 S0A\nT0\nM621 S0A\n", ""))
    assert run(hub, use_ams=False)[0] == 200


def test_a_file_for_another_model_is_refused(hub):
    put(hub)
    status, body = run(hub, serial="FAKE0002")  # a P1S
    assert status == 409 and "sliced for 'Bambu Lab H2S' but the target printer is 'P1S'" in body["error"]


def test_the_bridge_error_text_reaches_the_caller_and_the_sensor(hub):
    put(hub)
    FakeBridge.post_answer = {"/api/print": (502, {"error": "cloud says no"})}
    status, body = run(hub)
    assert (status, body["error"]) == (502, "cloud says no")
    r = hub.app.sender.result(SERIAL)
    assert (r["result"], r["last_error"], r["last_path"]) == ("error", "cloud says no", None)
    FakeBridge.post_answer = {"/api/print": (500, {"other": 1})}
    assert run(hub)[1]["error"] == "Add-on answered HTTP 500: {'other': 1}"
    FakeBridge.post_answer = {"/api/print": (200, {"result": "weird"})}
    assert run(hub)[0] == 502


def test_an_unreachable_bridge_is_reported(hub, monkeypatch):
    put(hub)
    monkeypatch.setattr(hub.app, "bridge_printers", lambda: [{"serial": SERIAL, "dev_product_name": "H2S"}])
    hub.app.bridge = ("127.0.0.1", 1)
    status, body = run(hub)
    assert status == 502 and body["error"].startswith("Add-on not reachable: ")
    assert hub.app.sender.result(SERIAL)["result"] == "error"


def test_the_action_needs_the_secret(hub):
    assert hub.req("POST", "/api/actions/print_file", {"serial": SERIAL, "file": "x"}, user="u-admin")[0] == 403


def test_the_queue_sends_through_the_app_without_a_home_assistant_service_call(hub):
    hub.ha.s["sensor.lab_h2s_print_status"]["state"] = "idle"
    hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL}, user="u-admin")
    status, job = hub.req("POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user="u-admin")
    assert status == 200 and job["state"] == "starting"
    assert [p["filename"] for p in prints()] == ["a.gcode.3mf"]
    assert [c for c in hub.ha.calls if c[0] == "druckwerk"] == []


def test_a_guard_refusal_keeps_the_queued_job_with_the_text(hub):
    make_3mf(os.path.join(hub.s.SHARE, "a.gcode.3mf"), gcode="; printer_model = Bambu Lab H2S\n")
    hub.ha.s["sensor.lab_h2s_print_status"]["state"] = "idle"
    hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL}, user="u-admin")
    status, body = hub.req("POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user="u-admin")
    assert status == 409 and body["error"].startswith("Refusing to send a.gcode.3mf") and prints() == []
    assert hub.app.queue.jobs[0]["state"] == "queued" and "Refusing" in hub.app.queue.jobs[0]["error"]


def test_the_browser_slice_prints_through_the_app_too(hub):
    jid = hub.sliced(owner="u-admin")
    status, body = hub.req("POST", "/api/jobs/%s/print" % jid, {"confirm": True, "timelapse": True}, user="u-admin")
    assert status == 200 and body["answer"] == {"result": "sent"}
    (sent,) = prints()
    assert sent["serial"] == SERIAL and sent["timelapse"] is True and sent["filename"].endswith(".gcode.3mf")
    assert [c for c in hub.ha.calls if c[0] == "druckwerk"] == []
    FakeBridge.printer_extra = {SERIAL: {"print_status": "RUNNING"}}
    status, body = hub.req("POST", "/api/jobs/%s/print" % jid, {"confirm": True}, user="u-admin")
    assert status == 409 and "refusing to send a new print" in body["error"]
