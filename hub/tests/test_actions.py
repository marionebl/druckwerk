"""Actions and LLM tools of the integration: the real app behind its HTTP routes, fake HA, bridge and slicer."""
import json
import os
import time

import pytest

import actions
from hubfakes import ADMIN, SERIAL, SERIAL2, USER, make_3mf

ACT = "X-Druckwerk-Act-As"
HERE = os.path.dirname(os.path.abspath(__file__))
FLAGS = {"plate_clear": True, "confirmed_by_person": True}


@pytest.fixture
def hub(hub, tmp_path):
    """The shared hub with printers (its media folder is inside the test directory)."""
    hub.setup_printers()
    hub.seed_share()
    return hub


def post(hub, path, data=None, user=None, secret=True):
    return hub.req("POST", path, data if data is not None else {}, secret=secret, headers={ACT: user} if user else None)


def act(hub, name, data=None, user=None):
    return post(hub, "/api/actions/" + name, data, user)


def tool(hub, name, data=None, user=None):
    return post(hub, "/api/tools/" + name, data, user)


def ok(answer):
    status, body = answer
    assert status == 200, body
    return body["result"]


def add(hub, name="a.gcode.3mf", target=SERIAL, user=None):
    return ok(act(hub, "add_to_queue", {"file": name, "target": target}, user))["job"]


# ---- reads
def test_get_printers_filters_by_serial_or_name_and_follows_the_user(hub):
    hub.grant(USER, SERIAL)
    assert [p["serial"] for p in ok(act(hub, "get_printers"))["printers"]] == [SERIAL, SERIAL2]
    assert [p["serial"] for p in ok(act(hub, "get_printers", {"printer": "lab"}))["printers"]] == [SERIAL]
    assert [p["serial"] for p in ok(act(hub, "get_printers", {"printer": SERIAL2}))["printers"]] == [SERIAL2]
    assert [p["serial"] for p in ok(act(hub, "get_printers", user=USER))["printers"]] == [SERIAL]  # act-as
    status, body = act(hub, "get_printers", {"printer": "nope"})
    assert status == 400 and body["error"] == "No printer nope visible to you"
    assert act(hub, "get_printers", {"printer": SERIAL2}, user=USER)[0] == 400  # not visible to this user


def test_get_queue_groups_waiting_jobs(hub):
    hub.grant(USER, SERIAL, needs_approval=True)
    first = add(hub)
    anywhere = ok(act(hub, "add_to_queue", {"file": "b.gcode.3mf", "model": "H2S"}))["job"]
    pending = add(hub, "c.gcode.3mf", user=USER)
    assert pending["state"] == "pending_approval"
    queue = ok(act(hub, "get_queue"))
    assert [(j["id"], j["position"]) for j in queue["waiting"][SERIAL]] == [(first["id"], 0), (pending["id"], 1)]
    assert [j["id"] for j in queue["waiting"]["any"]] == [anywhere["id"]]
    assert [j["id"] for j in queue["pending_approvals"]] == [pending["id"]] and queue["running"] == []
    hub.ha.s["sensor.lab_h2s_print_status"]["state"] = "idle"
    ok(act(hub, "start_next", dict(printer=SERIAL, **FLAGS)))
    ok(act(hub, "cancel_job", {"job_id": anywhere["id"]}))
    queue = ok(act(hub, "get_queue", {"history_limit": 5}))
    assert [j["id"] for j in queue["running"]] == [first["id"]]
    assert [j["id"] for j in queue["waiting"][SERIAL]] == [pending["id"]]
    assert [j["id"] for j in queue["history"]] == [anywhere["id"]]
    assert ok(act(hub, "get_queue"))["history"] == []


# ---- add to queue
def test_add_to_queue_by_file_name_and_options(hub):
    job = ok(act(hub, "add_to_queue", {"file": "cube.gcode.3mf", "target": "Lab", "name": "Benchy",
                                       "options": {"timelapse": True, "bed_leveling": "auto"}}))["job"]
    assert job["target"] == SERIAL and job["name"] == "Benchy" and job["options"]["timelapse"] is True
    assert job["options"]["bed_leveling"] == "auto"


def test_add_to_queue_absolute_media_path(hub):
    make_3mf(hub.media / "plate.gcode.3mf")
    job = ok(act(hub, "add_to_queue", {"file": str(hub.media / "plate.gcode.3mf"), "target": SERIAL}))["job"]
    assert job["grams"] == 75.22 and os.path.isfile(os.path.join(hub.s.SHARE, job["file"]))


def test_add_to_queue_from_a_slice_as_the_user(hub):
    hub.grant(USER, SERIAL)
    jid = hub.sliced(owner=USER)
    job = ok(act(hub, "add_to_queue", {"slice_job_id": jid, "target": "Lab"}, user=USER))["job"]
    assert job["owner_user_id"] == USER and job["target"] == SERIAL and job["grams"] == 75.22


def test_add_to_queue_any_needs_a_model_when_printers_differ(hub):
    status, body = act(hub, "add_to_queue", {"file": "a.gcode.3mf"})
    assert status == 400 and body["error"].startswith("target any needs model")
    assert ok(act(hub, "add_to_queue", {"file": "a.gcode.3mf", "model": "H2S"}))["job"]["target"] == "any"
    hub.app.registry.remove(SERIAL2)  # one model left: the action infers it
    assert ok(act(hub, "add_to_queue", {"file": "b.gcode.3mf"}))["job"]["model"] == "H2S"


@pytest.mark.parametrize("data", [{}, {"file": "a.gcode.3mf", "slice_job_id": "a1b2c3d4e5f6"}])
def test_add_to_queue_needs_exactly_one_source(hub, data):
    status, body = act(hub, "add_to_queue", data)
    assert status == 400 and "exactly one" in body["error"]


def test_access_rules_reach_the_caller_with_their_status(hub):
    status, body = act(hub, "add_to_queue", {"file": "a.gcode.3mf", "target": SERIAL}, user=USER)  # no grant
    assert status == 403 and body["error"].startswith("rule:")
    assert act(hub, "add_to_queue", {"file": "a.gcode.3mf", "target": SERIAL}, user="nobody")[0] == 403
    assert act(hub, "cancel_job", {"job_id": "0" * 12})[0] == 404


# ---- queue management
def test_job_actions_return_the_job(hub):
    hub.grant(USER, SERIAL, needs_approval=True)
    first, second = add(hub), add(hub, "b.gcode.3mf")
    pending = add(hub, "c.gcode.3mf", user=USER)
    assert ok(act(hub, "skip_job", {"job_id": first["id"]}))["job"]["id"] == first["id"]
    assert ok(act(hub, "approve_job", {"job_id": pending["id"]}))["job"]["state"] == "queued"
    assert ok(act(hub, "cancel_job", {"job_id": second["id"]}))["job"]["state"] == "cancelled"
    assert ok(act(hub, "retry_job", {"job_id": second["id"]}))["job"]["state"] == "queued"
    other = add(hub, "d.gcode.3mf", user=USER)
    assert ok(act(hub, "reject_job", {"job_id": other["id"]}))["job"]["state"] == "cancelled"
    assert act(hub, "skip_job", {"job_id": first["id"]}, user=USER)[0] == 403  # only admins skip


def test_reorder_and_move(hub):
    one, two = add(hub), add(hub, "b.gcode.3mf")
    ok(act(hub, "reorder_job", {"job_id": two["id"], "position": 0, "printer": "Lab"}))
    assert [j["id"] for j in ok(act(hub, "get_queue"))["waiting"][SERIAL]] == [two["id"], one["id"]]
    ok(act(hub, "reorder_job", {"job_id": two["id"], "position": 1}))
    assert [j["id"] for j in ok(act(hub, "get_queue"))["waiting"][SERIAL]] == [one["id"], two["id"]]
    assert ok(act(hub, "move_job", {"job_id": one["id"], "target": "any", "model": "H2S"}))["job"]["target"] == "any"
    assert ok(act(hub, "move_job", {"job_id": one["id"], "target": "Lab"}))["job"]["target"] == SERIAL


# ---- start needs both confirmations
@pytest.mark.parametrize("flags", [{}, {"plate_clear": True}, {"confirmed_by_person": True},
                                   {"plate_clear": True, "confirmed_by_person": False}])
@pytest.mark.parametrize("name", ["start_next", "start_job"])
def test_start_is_refused_without_both_flags(hub, name, flags):
    job = add(hub)
    hub.ha.s["sensor.lab_h2s_print_status"]["state"] = "idle"
    data = dict({"printer": SERIAL}, **flags, **({"job_id": job["id"]} if name == "start_job" else {}))
    status, body = act(hub, name, data)
    assert status == 400 and "camera snapshot" in body["error"] and "explicit yes" in body["error"]
    assert hub.sent() == []
    assert ok(act(hub, "get_queue"))["waiting"][SERIAL][0]["state"] == "queued"


def test_start_next_and_start_job_with_both_flags(hub):
    one, two = add(hub), add(hub, "b.gcode.3mf")
    hub.ha.s["sensor.lab_h2s_print_status"]["state"] = "idle"
    assert ok(act(hub, "start_next", dict(printer="Lab", **FLAGS)))["job"]["id"] == one["id"]
    hub.app.queue.jobs[0].update(state="done", printer=None)
    assert ok(act(hub, "start_job", dict(printer=SERIAL, job_id=two["id"], **FLAGS)))["job"]["state"] == "starting"
    assert [s["filename"] for s in hub.sent()] == ["a.gcode.3mf", "b.gcode.3mf"]


def test_start_is_for_admins_even_with_both_flags(hub):
    hub.grant(USER, SERIAL)
    add(hub, user=USER)
    status, body = act(hub, "start_next", dict(printer=SERIAL, **FLAGS), user=USER)
    assert status == 403 and body["error"].startswith("rule:")


# ---- slicing with a fake slicer
@pytest.fixture
def slicer(hub, monkeypatch):
    """Three slices that end after a short pause, with the real slice_info of the test fixture."""
    s = hub.s
    printer = {"serial": SERIAL, "name": "Lab", "model": "H2S", "bed": "Textured PEI Plate", "qualities": ["good", "better", "best"],
               "trays": [{"ams": "0", "id": "0", "type": "PETG", "idx": "", "name": "PETG", "colour": "#ADB1B2"}], "online": True}
    monkeypatch.setattr(s, "printers", lambda: [printer])
    monkeypatch.setattr(actions, "SLICE_POLL", 0.02)

    def run_slice(jid, printer, quality, tray, bed):
        job = s.jobs[jid]
        time.sleep(0.1)
        result = make_3mf(hub.tmp / ("%s.gcode.3mf" % jid))
        job.update(state="done", result=result, info=s.slice_info(result), settings={"process": "0.20mm Standard @X", "tray": tray})

    monkeypatch.setattr(s, "run_slice", run_slice)
    (hub.media / "cube.stl").write_bytes(b"solid x")
    return hub


def test_slice_waits_for_three_variants_and_saves_the_plate_images(slicer):
    hub = slicer
    hub.grant(USER, SERIAL)
    data = {"model_file": str(hub.media / "cube.stl"), "printer": "Lab", "tray": "0:0", "plate": "Textured PEI Plate"}
    res = ok(act(hub, "slice", data, user=USER))
    assert res["state"] == "done" and list(res["slice_job_ids"]) == ["good", "better", "best"]
    assert [v["quality"] for v in res["variants"]] == ["good", "better", "best"]
    first = res["variants"][0]
    assert first["print_time"] == "2 h 04 min" and first["grams"] == 75.22 and first["layer_height"] == "0.20mm"
    assert first["plate_image"] == str(hub.media / "druckwerk" / "plates" / (first["slice_job_id"] + ".png"))
    assert open(first["plate_image"], "rb").read().startswith(b"\x89PNG")
    assert all(hub.s.jobs[i]["owner_user_id"] == USER for i in res["slice_job_ids"].values())


def test_slice_without_waiting_returns_ids_and_get_slice_reads_them(slicer):
    hub = slicer
    data = {"model_file": str(hub.media / "cube.stl"), "printer": SERIAL, "tray": "0:0", "wait": False}
    res = ok(act(hub, "slice", data))
    assert res["state"] == "slicing" and res["variants"] == [] and len(res["slice_job_ids"]) == 3
    ids = list(res["slice_job_ids"].values())
    got = ok(act(hub, "get_slice", {"slice_job_ids": ids}))
    assert got["state"] == "slicing" and [v["state"] for v in got["variants"]] != []
    deadline = time.time() + 5
    while got["state"] != "done" and time.time() < deadline:
        time.sleep(0.05)
        got = ok(act(hub, "get_slice", {"slice_job_ids": ids[0]}))  # a single id is a list of one
    assert got["state"] == "done" and got["variants"][0]["plate_image"]


def test_slice_gives_up_waiting_but_the_slices_go_on(slicer, monkeypatch):
    monkeypatch.setattr(actions, "SLICE_WAIT", 0.0)
    status, body = act(slicer, "slice", {"model_file": str(slicer.media / "cube.stl"), "printer": SERIAL, "tray": "0:0"})
    assert status == 504 and "continues in the app" in body["error"]


@pytest.mark.parametrize("path, tray, text", [("/etc/passwd", "0:0", "inside"), ("rel.stl", "0:0", "absolute"),
                                              ("x.txt", "0:0", "must be one of"), ("gone.stl", "0:0", "No such file"),
                                              ("cube.stl", "bad", "unit:slot")])
def test_slice_rejects_bad_input_before_it_uploads(hub, path, tray, text):
    (hub.media / "x.txt").write_text("x")
    (hub.media / "cube.stl").write_bytes(b"s")
    full = path if path in ("/etc/passwd", "rel.stl") else str(hub.media / path)
    status, body = act(hub, "slice", {"model_file": full, "printer": SERIAL, "tray": tray})
    assert status == 400 and text in body["error"]
    assert hub.s.jobs == {}


# ---- camera
def test_camera_snapshot_is_saved_in_media(hub):
    res = ok(act(hub, "camera_snapshot", {"printer": "Lab"}))
    path = res["path"]
    assert os.path.dirname(path) == str(hub.media / "druckwerk") and os.path.basename(path).startswith(SERIAL + "-")
    assert open(path, "rb").read() == b"FRAME:/api/camera/%s/snapshot" % SERIAL.encode() and res["size_bytes"] == os.path.getsize(path)
    assert res["media_content_id"] == "media-source://media_source/local/druckwerk/" + os.path.basename(path)
    assert res["printer"] == SERIAL and len(res["taken_at"]) == 15


def test_snapshots_are_pruned_per_printer(hub):
    folder = hub.media / "druckwerk"
    for i in range(actions.SNAPSHOT_KEEP + 3):
        (folder / f"{SERIAL}-2020010{i:02d}.jpg").write_bytes(b"x")
    (folder / f"{SERIAL2}-20200101.jpg").write_bytes(b"x")
    ok(act(hub, "camera_snapshot", {"printer": SERIAL}))
    names = os.listdir(folder)
    assert len([n for n in names if n.startswith(SERIAL)]) == actions.SNAPSHOT_KEEP and f"{SERIAL2}-20200101.jpg" in names


def test_camera_needs_a_grant(hub):
    status, body = act(hub, "camera_snapshot", {"printer": SERIAL}, user=USER)
    assert status == 403 and body["error"].startswith("rule:")


# ---- act-as and access
def test_routes_need_the_secret(hub):
    for method, path in (("GET", "/api/integration/manifest"), ("POST", "/api/actions/get_queue"), ("POST", "/api/tools/druckwerk_get_queue")):
        assert hub.req(method, path, {} if method == "POST" else None)[0] == 401
        assert hub.req(method, path, {} if method == "POST" else None, user=ADMIN)[0] == 403
        assert hub.req(method, path, {} if method == "POST" else None, secret=True, headers={ACT: "nobody"})[0] == 403


def test_the_act_as_user_owns_what_an_action_creates(hub):
    hub.grant(USER, SERIAL)
    job = add(hub, user=USER)
    assert job["owner_name"] == "Ben" and hub.app.queue.jobs[0]["owner_user_id"] == USER
    assert ok(act(hub, "cancel_job", {"job_id": job["id"]}, user=USER))["job"]["state"] == "cancelled"
    other = add(hub, "b.gcode.3mf")  # no header: the automation is admin
    assert act(hub, "cancel_job", {"job_id": other["id"]}, user=USER)[0] == 403


# ---- tools
def test_a_tool_runs_the_action_as_the_user(hub):
    hub.grant(USER, SERIAL)
    job = add(hub, user=USER)
    assert [p["serial"] for p in ok(tool(hub, "druckwerk_get_printers", {}, USER))["printers"]] == [SERIAL]
    assert ok(tool(hub, "druckwerk_get_queue", {}, USER))["waiting"][SERIAL][0]["id"] == job["id"]
    assert ok(tool(hub, "druckwerk_cancel_job", {"job_id": job["id"]}, USER))["job"]["state"] == "cancelled"
    assert ok(tool(hub, "druckwerk_get_slice_results", {"slice_job_ids": []}))["variants"] == []


def test_a_start_tool_is_refused_without_both_flags(hub):
    add(hub)
    hub.ha.s["sensor.lab_h2s_print_status"]["state"] = "idle"
    status, body = tool(hub, "druckwerk_start_next", {"printer": SERIAL, "plate_clear": True})
    assert status == 400 and "camera snapshot" in body["error"]
    assert ok(tool(hub, "druckwerk_start_next", dict(printer=SERIAL, **FLAGS)))["job"]["state"] == "starting"


def test_tools_are_strict_where_actions_coerce(hub):
    job = add(hub)
    assert tool(hub, "druckwerk_cancel_job", {"job_id": 5})[1]["error"] == "expected str for dictionary value @ data['job_id']"
    assert act(hub, "cancel_job", {"job_id": 5})[0] == 404  # coerced to "5", which is no job
    assert tool(hub, "druckwerk_get_queue", {"history_limit": "3"})[0] == 400
    assert act(hub, "get_queue", {"history_limit": "3"})[0] == 200
    assert tool(hub, "druckwerk_add_to_queue", {"file": "a.gcode.3mf", "options": {"timelapse": "yes"}})[0] == 400
    assert act(hub, "add_to_queue", {"file": "a.gcode.3mf", "target": SERIAL, "options": {"timelapse": "yes"}})[1]["result"]["job"]["options"]["timelapse"] is True
    assert ok(tool(hub, "druckwerk_cancel_job", {"job_id": job["id"]}))["job"]["id"] == job["id"]


# ---- validation
@pytest.mark.parametrize("name, data, text", [
    ("cancel_job", {}, "required key not provided @ data['job_id']"),
    ("cancel_job", {"job_id": "a" * 12, "x": 1}, "extra keys not allowed @ data['x']"),
    ("get_queue", {"history_limit": 51}, "value must be at most 50 for dictionary value @ data['history_limit']"),
    ("get_queue", {"history_limit": -1}, "value must be at least 0 for dictionary value @ data['history_limit']"),
    ("get_queue", {"history_limit": "many"}, "expected int for dictionary value @ data['history_limit']"),
    ("reorder_job", {"job_id": "a" * 12, "position": -1}, "value must be at least 0 for dictionary value @ data['position']"),
    ("slice", {"model_file": "/media/a.stl", "printer": SERIAL, "tray": "0:0", "wait": "maybe"},
     "invalid boolean value maybe for dictionary value @ data['wait']"),
    ("add_to_queue", {"file": "a", "options": {"bed_type": "gold"}}, "value must be one of"),
    ("add_to_queue", {"file": "a", "options": {"gold": 1}}, "extra keys not allowed @ data['options']['gold']"),
    ("add_to_queue", {"file": "a", "name": "x" * 81}, "length of value must be at most 80"),
    ("get_printers", {"printer": ["a"]}, "value should be a string"),
    ("print_file", {"file": "a.gcode.3mf"}, "must contain at least one of device_id, serial."),
])
def test_validation_errors_are_400(hub, name, data, text):
    status, body = act(hub, name, data)
    assert status == 400 and text in body["error"], body


def test_unknown_names_and_bad_bodies(hub):
    assert act(hub, "nope")[0] == 404 and tool(hub, "druckwerk_nope")[0] == 404
    assert hub.req("POST", "/api/actions/get_queue", raw=b"[1]", secret=True)[0] == 400
    assert hub.req("POST", "/api/actions/get_queue", raw=b"{", secret=True)[0] == 400


def test_an_action_reads_yes_and_1_as_true_and_off_as_false(hub):
    add(hub)
    hub.ha.s["sensor.lab_h2s_print_status"]["state"] = "idle"
    assert act(hub, "start_next", {"printer": SERIAL, "plate_clear": "off", "confirmed_by_person": "on"})[0] == 400
    assert ok(act(hub, "start_next", {"printer": SERIAL, "plate_clear": "yes", "confirmed_by_person": 1}))["job"]["state"] == "starting"


# ---- manifest
def manifest(hub):
    status, body = hub.req("GET", "/api/integration/manifest", secret=True)
    assert status == 200
    return body


def test_manifest_has_a_stable_revision_that_follows_the_content(hub, monkeypatch):
    first, second = manifest(hub), manifest(hub)
    assert first == second and first["api_version"] == 1 and len(first["revision"]) == 12
    body = {k: v for k, v in first.items() if k != "revision"}
    assert first["revision"] == actions.revision(body)
    monkeypatch.setitem(actions.ACTIONS["get_queue"], "description", "Changed.")
    changed = manifest(hub)
    assert changed["revision"] != first["revision"]
    assert next(a for a in changed["actions"] if a["name"] == "get_queue")["description"] == "Changed."


def test_events_carry_the_manifest_revision(hub):
    rev = manifest(hub)["revision"]
    first = hub.req("GET", "/api/events", secret=True)[1]
    since = hub.req("GET", "/api/events?since=%d" % first["seq"], secret=True)[1]
    assert first["manifest_revision"] == since["manifest_revision"] == rev


def test_manifest_describes_what_the_ui_needs(hub):
    by_name = {a["name"]: a for a in manifest(hub)["actions"]}
    printer = by_name["slice"]["fields"]["plate"]
    assert printer["selector"]["select"]["options"][3] == "Textured PEI Plate" and printer["example"] == "Textured PEI Plate"
    assert by_name["print_file"]["fields"]["device_id"]["selector"] == {"device": {"filter": [{"manufacturer": "Bambu Lab"}]}}
    assert by_name["get_queue"]["fields"]["history_limit"]["selector"]["number"]["max"] == 50
    assert by_name["start_job"]["serial_fields"] == ["printer"] and by_name["move_job"]["serial_fields"] == ["target"]
    tools = {t["name"]: t for t in manifest(hub)["llm"]["tools"]}
    start = tools["druckwerk_start_next"]
    assert start["annotations"] == {"read_only": False, "destructive": True, "idempotent": False, "open_world": True}
    assert all("type" in p or "anyOf" in p for t in tools.values() for p in t["parameters"]["properties"].values())
    assert "plate_clear" in manifest(hub)["llm"]["prompt"] and json.dumps(manifest(hub))


# ---- golden contract of the integration
def test_manifest_matches_the_golden_contract(hub):
    with open(os.path.join(HERE, "fixtures", "contract_golden.json")) as f:
        golden = json.load(f)
    got = manifest(hub)
    assert {a["name"] for a in got["actions"]} == set(golden["services"])
    for a in got["actions"]:
        want = golden["services"][a["name"]]
        assert a["response"] == want["response"], a["name"]  # the golden spells SupportsResponse as "only" and "optional"
        assert set(a["fields"]) == set(want["fields"]), a["name"]
        for key, field in a["fields"].items():
            spec = want["fields"][key]
            assert field["required"] == spec.get("required", False), (a["name"], key)
            assert ("default" in field) == ("default" in spec) and field.get("default") == spec.get("default"), (a["name"], key)
    tools = {t["name"]: t for t in got["llm"]["tools"]}
    assert set(tools) == set(golden["llm_tools"])
    for name, want in golden["llm_tools"].items():
        assert sorted(tools[name]["parameters"]["properties"]) == want["properties"], name
        assert sorted(tools[name]["parameters"]["required"]) == want["required"], name
