"""The steps between Start and the first layer (hub/progress.py) and how the printer list serves them."""
import progress
import printer_state
from hubfakes import ADMIN, SERIAL, FakeBridge


def states(steps):
    return [(s["id"], s["state"]) for s in steps]


def test_no_steps_when_nothing_is_starting():
    assert progress.steps(None, None, {"status": "idle", "stage": "idle"}) is None
    assert progress.steps(None, None, {"status": "running", "stage": "printing", "prepare_percent": 100}) is None
    assert progress.steps(None, None, None) is None


def test_upload_is_the_active_step_while_the_hub_sends_the_file():
    up = {"stage": "upload", "percent": 45}
    steps = progress.steps("a.gcode.3mf", up, {"status": "idle", "stage": "idle"})
    assert states(steps) == [("upload", "active"), ("download", "pending"), ("prepare", "pending"), ("print", "pending")]
    assert steps[0]["percent"] == 45
    # Before the first upload line the bridge has only a stage; the step says what it does and shows no percent.
    waiting = progress.steps("a.gcode.3mf", {"stage": "waitprinter", "percent": None}, None)
    assert waiting[0]["percent"] is None and waiting[0]["detail"] == "Waiting for the printer"


def test_download_shows_the_printers_percent_then_prepare_shows_the_stage():
    v = {"status": "prepare", "stage": "idle", "prepare_percent": 37}
    steps = progress.steps(None, None, v)
    assert states(steps) == [("upload", "done"), ("download", "active"), ("prepare", "pending"), ("print", "pending")]
    assert steps[1]["percent"] == 37
    steps = progress.steps(None, None, dict(v, prepare_percent=100, stage="heatbed_preheating"))
    assert states(steps) == [("upload", "done"), ("download", "done"), ("prepare", "active"), ("print", "pending")]
    assert steps[2]["detail"] == "heatbed_preheating"
    # Stages arrive with gcode_state running on some printers
    assert progress.steps(None, None, {"status": "running", "stage": "auto_bed_leveling", "prepare_percent": None})[2]["state"] == "active"


def test_prepare_percent_comes_from_the_status_field():
    # Fake serial and values; the field name follows the vendor's MQTT report.
    v = printer_state.view({"gcode_state": "PREPARE", "gcode_file_prepare_percent": "62", "stg_cur": -1}, 0)
    assert (v["status"], v["prepare_percent"]) == ("prepare", 62)
    assert printer_state.view({"gcode_state": "IDLE"}, 0)["prepare_percent"] is None


def test_printer_list_serves_steps_while_the_printer_downloads(hub):
    hub.setup_printers()
    assert hub.req("GET", "/api/printers", user=ADMIN)[1][0]["start"] is None
    hub.set_print(gcode_state="PREPARE", stg_cur=-1, gcode_file_prepare_percent="37", mc_percent=0)
    p = hub.req("GET", "/api/printers", user=ADMIN)[1][0]
    assert states(p["start"])[1] == ("download", "active") and p["start"][1]["percent"] == 37


def test_printer_list_serves_the_upload_percent_while_the_hub_sends(hub):
    hub.setup_printers()
    FakeBridge.upload[SERIAL] = {"stage": "upload", "percent": 45}
    hub.app.sender.sending[SERIAL] = "a.gcode.3mf"
    p = hub.req("GET", "/api/printers", user=ADMIN)[1][0]
    assert states(p["start"])[0] == ("upload", "active") and p["start"][0]["percent"] == 45


def test_the_queued_job_that_is_being_sent_is_named(hub):
    hub.setup_printers()
    hub.seed_share()
    job = hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL}, user=ADMIN)[1]
    hub.app.sender.sending[SERIAL] = "a.gcode.3mf"
    p = hub.req("GET", "/api/printers", user=ADMIN)[1][0]
    assert p["start_job"] == job["id"]
