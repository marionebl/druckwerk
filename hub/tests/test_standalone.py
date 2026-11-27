"""Druckwerk without the vendor's Home Assistant integration: printers, state, queue and cover come from the bridge alone."""
import importlib.util
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hubfakes import ADMIN, GCODE, HERE, REPO, SERIAL, FakeBridge, Hub, make_3mf, task

LIVE = json.load(open(os.path.join(HERE, "fixtures", "h2s_finished_status.json")))["print"]
# The vendor integration's values for the same printer at the same moment (sensor.<prefix>_*, live 2026-11-27 18:10 UTC).
VENDOR_VALUES = {"print_status": "finish", "current_stage": "idle", "print_progress": 100, "remaining_time": 0.0,  # hours
                 "end_time": "unknown", "current_layer": 831, "total_layer_count": 831, "nozzle_temperature": 29,
                 "nozzle_target_temperature": 0, "bed_temperature": 25, "bed_target_temperature": 0, "chamber_temperature": 26,
                 "active_tray": "none", "task_name": "vase_21cm_2026-11-26_h2s_good_arc", "hms_errors": "off", "online": "on",
                 "humidity_index": 1, "ams_temperature": 62.0}


def bridge_drying_units(status):
    """The bridge's own answer for GET /api/drying/<serial>, computed by its real code."""
    os.environ.setdefault("DW_DATA", "/tmp/dw-standalone-test")
    spec = importlib.util.spec_from_file_location("bridge_server", os.path.join(REPO, "hub", "bridge", "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.drying_units({"print": status})


@pytest.fixture
def bare(tmp_path):
    """Home Assistant without the vendor integration (no devices, no entities of it), a bridge that reports the live H2S."""
    h = Hub(tmp_path, vendor=False)
    FakeBridge.status = {SERIAL: LIVE}
    FakeBridge.drying = bridge_drying_units(LIVE)
    yield h
    h.server.shutdown()
    h.bridge.shutdown()


def printers(h):
    return {p["serial"]: p for p in h.req("GET", "/api/printers", user=ADMIN)[1]}


def test_a_printer_registers_and_reports_without_the_vendor_integration(bare):
    assert bare.ha.devices == [] and bare.ha.entities == [] and not any(k.startswith("sensor.") for k in bare.ha.s)
    assert bare.req("GET", "/api/printers", user=ADMIN)[1] == []
    assert bare.req("GET", "/api/setup", user=ADMIN)[0] == 200  # registers the bridge's printers
    assert bare.app.registry.get(SERIAL), "the printer was not registered"
    assert bare.app.registry.get(SERIAL)["prefix"] == "lab"
    p = printers(bare)[SERIAL]
    assert (p["online"], p["status"], p["stage"], p["progress"], p["remaining_min"], p["end_time"]) == (True, "finish", "idle", 100.0, 0, None)
    assert (p["layer"], p["total_layers"]) == (831, 831)
    assert (p["nozzle_temp"], p["nozzle_target"], p["bed_temp"], p["bed_target"], p["chamber_temp"]) == (29, 0, 25, 0, 25)
    assert (p["task_name"], p["active_tray"], p["hms"], p["external"]) == ("vase_21cm_2026-11-26_h2s_good_arc", None, [], None)
    assert p["ams"] == [{"unit": 0, "humidity_index": 1, "temp": 62.0, "trays": [
        {"slot": s, "type": "", "colour": "", "name": "", "remain_pct": None, "spool_id": None} for s in range(4)]}]
    assert len(p["drying"]) == 1
    assert (p["drying"][0]["name"], p["drying"][0]["status"], p["drying"][0]["active"], p["drying"][0]["left_min"],
            p["drying"][0]["filament"], p["drying"][0]["temp"], p["drying"][0]["hours"]) == ("AMS 2 Pro", "drying", True, 474, "PETG", 65, 12)


def test_the_bridge_values_equal_the_vendor_integrations_for_the_same_moment(bare):
    bare.req("GET", "/api/setup", user=ADMIN)
    p, v = printers(bare)[SERIAL], VENDOR_VALUES
    same = {  # field of /api/printers -> value the vendor integration shows
        "status": v["print_status"], "stage": v["current_stage"], "progress": v["print_progress"],
        "remaining_min": round(v["remaining_time"] * 60), "layer": v["current_layer"], "total_layers": v["total_layer_count"],
        "nozzle_temp": v["nozzle_temperature"], "nozzle_target": v["nozzle_target_temperature"],
        "bed_temp": v["bed_temperature"], "bed_target": v["bed_target_temperature"], "task_name": v["task_name"],
        "online": v["online"] == "on", "hms": [], "end_time": None, "active_tray": None}
    assert {k: p[k] for k in same} == same
    assert (p["hms"] == []) == (v["hms_errors"] == "off")
    assert p["ams"][0]["humidity_index"] == v["humidity_index"] and p["ams"][0]["temp"] == v["ams_temperature"]
    # The one difference: the vendor integration keeps an idle temperature that moved by 1 C for a while (its idle deadband).
    assert (p["chamber_temp"], v["chamber_temperature"]) == (25, 26)


def test_the_queue_starts_a_job_on_a_printer_without_vendor_entities(bare):
    bare.seed_share()
    bare.req("GET", "/api/setup", user=ADMIN)
    bare.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL}, user=ADMIN)
    st, job = bare.req("POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user=ADMIN)
    assert st == 200 and job["state"] == "starting"
    bare.set_print(gcode_state="RUNNING", stg_cur=4, ams={"tray_now": "0", "ams": [{"id": "0", "tray": [
        {"id": "0", "state": 11, "tray_type": "PLA", "tray_color": "FF0000FF"}]}]})
    bare.app.tick_all()
    assert bare.app.queue.jobs[0]["state"] == "printing"
    bare.set_print(gcode_state="FINISH")
    bare.app.tick_all()
    assert bare.app.queue.history[-1]["state"] == "done"


def test_hms_shows_the_code_the_severity_and_the_wiki_link(bare):
    bare.req("GET", "/api/setup", user=ADMIN)
    bare.set_print(hms=[{"attr": 0x07008000, "code": 0x00010003}, {"attr": 0x0C000300, "code": 0x0003000B}, {"attr": 0, "code": 0}])
    (e,) = printers(bare)[SERIAL]["hms"]  # the "inspecting the first layer" notice and the empty entry are no errors
    assert e == {"code": "HMS_0700_8000_0001_0003", "severity": "fatal", "text": "", "url": "https://wiki.bambulab.com/en/hms/home"}


def test_a_running_print_gives_end_time_and_the_active_tray(bare):
    bare.req("GET", "/api/setup", user=ADMIN)
    bare.set_print(gcode_state="RUNNING", mc_remaining_time=90, stg_cur=0, mc_percent=12, ams={"tray_now": "2", "ams": [{"id": "0", "tray": [
        {"id": "2", "state": 11, "tray_type": "PETG", "tray_sub_brands": "PETG HF", "tray_color": "0A2989FF", "remain": 70}]}]})
    p = printers(bare)[SERIAL]
    assert (p["status"], p["stage"], p["progress"], p["remaining_min"]) == ("running", "printing", 12.0, 90)
    assert p["end_time"] == "1970-01-01T01:46:00+00:00"  # the fake clock stands at 1000 s
    assert p["active_tray"] == {"unit": 0, "slot": 2}
    assert p["ams"][0]["trays"][0] == {"slot": 2, "type": "PETG", "colour": "0A2989", "name": "PETG HF", "remain_pct": 70, "spool_id": None}


# ---- cover image
def cover(h):
    return h.req("GET", "/api/printers/%s/cover" % SERIAL, user=ADMIN)


class CloudImage(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.end_headers()
        self.wfile.write(b"CLOUDCOVER")

    def log_message(self, *a):
        pass


def test_cover_is_the_plate_of_the_sent_file_else_the_cloud_cover_else_none(bare):
    bare.req("GET", "/api/setup", user=ADMIN)
    assert cover(bare)[0] == 404  # the printer's task is not a file here and the cloud has no task for it
    srv = ThreadingHTTPServer(("127.0.0.1", 0), CloudImage)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        title = LIVE["subtask_name"]
        FakeBridge.tasks = {SERIAL: [dict(task("1", title, 10), cover_url="http://127.0.0.1:%d/c.jpg" % srv.server_address[1])]}
        bare.app._cache.clear()
        assert cover(bare) == (200, b"CLOUDCOVER")
        FakeBridge.tasks = {SERIAL: [dict(task("1", "another print", 10), cover_url="http://127.0.0.1:1/x")]}
        bare.app._cache.clear()
        assert cover(bare)[0] == 404
    finally:
        srv.shutdown()
    make_3mf(bare.media / "druckwerk" / (LIVE["subtask_name"] + ".gcode.3mf"), gcode=GCODE)
    bare.app._cache.clear()
    assert cover(bare) == (200, b"\x89PNG\r\n\x1a\nfake")  # the plate image of the file named like the task
    assert printers(bare)[SERIAL]["cover_url"] == "api/printers/%s/cover" % SERIAL
    bare.set_print(subtask_name="")
    assert printers(bare)[SERIAL]["cover_url"] is None


def test_every_module_the_app_imports_is_copied_into_the_image():
    """The Dockerfile lists the app's files one by one; a new module that is not listed would crash the app at start."""
    docker = open(os.path.join(REPO, "hub", "Dockerfile")).read()
    for name in ("app", "integration", "watchdog", "printer_state"):
        src = open(os.path.join(REPO, "hub", name + ".py")).read()
        for mod in re.findall(r"^(?:import|from) (\w+)", src, re.M):
            if os.path.exists(os.path.join(REPO, "hub", mod + ".py")):
                assert "hub/%s.py" % mod in docker, mod
