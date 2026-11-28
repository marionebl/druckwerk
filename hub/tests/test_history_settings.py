"""History detail: the settings a print was made with, and a new print job with the same settings."""
import os
import shutil

import slice_settings
from hubfakes import ADMIN, HERE, OTHER, SERIAL, USER, FakeBridge, task

FIXTURE = os.path.join(HERE, "fixtures", "settings.gcode.3mf")  # anonymized: no serials, accounts or ids


def row(data, key):
    return next(r for g in data["groups"] for r in g["rows"] if r["key"] == key)


def scene(hub, slice_alive=True):
    """A finished Druckwerk print (task 7, job j1) whose slice the slicer keeps, and a cloud-only task 8."""
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    jid = hub.sliced(owner=USER)
    shutil.copyfile(FIXTURE, hub.s.jobs[jid]["result"])
    FakeBridge.tasks = {SERIAL: [task(7, "cube", 9000), task(8, "vase", 6000)]}
    hub.app.queue.history.append({
        "id": "j1", "file": "cube.gcode.3mf", "source": "cube.gcode.3mf", "target": SERIAL, "model": "", "filaments": [], "error": None,
        "options": {"timelapse": True, "flow_cali": "auto"}, "state": "done", "owner_user_id": USER, "owner_name": "BEN", "created_at": 8990,
        "printer": SERIAL, "started_at": 8995, "finished_at": 9600, "name": "Cube for Ben", "slice_job": jid})
    if not slice_alive:
        hub.s.jobs.pop(jid)
    return jid


def test_slicing_settings_of_a_print(hub):
    scene(hub)
    st, d = hub.req("GET", "/api/history/7/settings", user=USER)
    assert st == 200 and d["available"] and d["source"] == "slice", d
    assert row(d, "layer_height")["value"] == "0.2" and row(d, "layer_height")["unit"] == "mm"
    assert row(d, "sparse_infill_density")["value"] == "15%"
    assert row(d, "filament_type")["value"] == "PETG" and row(d, "@bed_temp")["value"] == "70"
    assert [g["title"] for g in d["groups"]][:3] == ["Quality", "Strength", "Support and adhesion"]
    assert ["layer_height", "0.2"] in d["all"] and len(d["all"]) > 100
    assert d["can_reprint"] is True


def test_settings_follow_the_same_visibility_as_the_history(hub):
    scene(hub)
    assert hub.req("GET", "/api/history/7/settings", user=OTHER)[0] == 404
    assert hub.req("GET", "/api/history/7/settings")[0] == 401
    assert hub.req("GET", "/api/history/nope/settings", user=ADMIN)[0] == 404


def test_settings_are_kept_after_the_slicer_forgot_the_slice(hub):
    jid = scene(hub)
    st, queued = hub.req("POST", "/api/jobs/%s/queue" % jid, {"target": SERIAL}, user=USER)
    assert st == 200
    hub.app.queue.history.append(dict(hub.app.queue.jobs.pop(), state="done", printer=SERIAL, started_at=9100, finished_at=9500,
                                      source="cube.gcode.3mf", file=queued["file"]))
    hub.s.jobs.pop(jid)
    st, d = hub.req("GET", "/api/history/job-%s/settings" % queued["id"], user=USER)
    assert st == 200 and d["available"] and d["source"] == "saved" and row(d, "layer_height")["value"] == "0.2", d
    assert d["can_reprint"] is False


def test_cloud_only_print_without_a_file_says_so(hub):
    scene(hub)
    st, d = hub.req("GET", "/api/history/8/settings", user=ADMIN)
    assert st == 200 and d["available"] is False and d["groups"] == [] and d["reason"], d
    assert d["task"]["title"] == "vase" and d["task"]["plate"] == 1 and d["can_reprint"] is False


def test_cloud_only_print_with_a_file_is_fetched_and_cached(hub, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    scene(hub)
    FakeBridge.tasks[SERIAL][1].update(model_id="M1", profile_id=55)
    FakeBridge.files = {"/dl/vase.gcode.3mf": open(FIXTURE, "rb").read()}
    FakeBridge.file_url = "http://127.0.0.1:%d/dl/vase.gcode.3mf" % hub.bridge.server_address[1]
    st, d = hub.req("GET", "/api/history/8/settings", user=ADMIN)
    assert st == 200 and d["available"] and d["source"] == "cloud" and row(d, "wall_loops")["value"] == "2", d
    assert d["can_reprint"] is True
    assert os.path.isfile(os.path.join(hub.app.data_dir, "cloud", "8.gcode.3mf"))
    FakeBridge.files = {}  # a second call needs no download
    assert hub.req("GET", "/api/history/8/settings", user=ADMIN)[1]["available"] is True


def test_new_print_job_from_a_history_item_with_the_same_options(hub):
    scene(hub)
    st, job = hub.req("POST", "/api/history/7/queue", {}, user=USER)
    assert st == 200, job
    assert job["target"] == SERIAL and job["options"]["timelapse"] is True and job["options"]["flow_cali"] == "auto"
    assert job["name"] == "Cube for Ben" and job["state"] == "queued" and job["slice_job"]
    assert os.path.isfile(os.path.join(hub.s.SHARE, job["file"]))
    assert hub.req("POST", "/api/history/7/queue", {}, user=OTHER)[0] == 404


def test_new_print_job_from_a_fetched_cloud_file(hub, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    scene(hub)
    hub.grant(ADMIN, SERIAL)
    FakeBridge.tasks[SERIAL][1].update(model_id="M1", profile_id=55)
    FakeBridge.files = {"/dl/vase.gcode.3mf": open(FIXTURE, "rb").read()}
    FakeBridge.file_url = "http://127.0.0.1:%d/dl/vase.gcode.3mf" % hub.bridge.server_address[1]
    st, job = hub.req("POST", "/api/history/8/queue", {}, user=ADMIN)
    assert st == 200, job
    assert job["target"] == SERIAL and job["name"] == "vase" and os.path.isfile(os.path.join(hub.s.SHARE, job["file"]))
    assert job["grams"] == 75.22 and job["filaments"] == [{"type": "PETG", "colour": "ADB1B2"}]


def test_no_file_no_new_job(hub):
    scene(hub)
    st, body = hub.req("POST", "/api/history/8/queue", {}, user=ADMIN)
    assert st == 409 and "file" in body["error"], body


def test_settings_module_reads_the_fixture():
    d = slice_settings.read(FIXTURE)
    assert d["plate"]["prediction"] == "7440" and d["slicer"] == "02.09.02.62"
    assert slice_settings.read(os.path.join(HERE, "fixtures", "slice_info.config")) is None
