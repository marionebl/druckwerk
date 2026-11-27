"""The generic integration: entity table, state, files, upload, camera, image, light, re-read."""
import io
import json
import os
import re
import zipfile

import pytest

import actions
import files
from hubfakes import ADMIN, ENTITIES, REPO, SERIAL, SERIAL2, USER, VENDOR, FakeBridge, make_3mf, st


@pytest.fixture
def hub(hub):
    hub.setup_printers()
    hub.seed_share()
    return hub


def get(hub, path):
    return hub.req("GET", path, secret=True)


def state(hub):
    status, body = get(hub, "/api/integration/state")
    assert status == 200, body
    return body


def me(hub, serial=SERIAL):
    return state(hub)["printers"][serial]["entities"]


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


# ---- the entity table in the manifest
def test_entity_table_reproduces_the_golden_entity_ids(hub):
    with open(os.path.join(REPO, "tests", "golden", "contract.json")) as f:
        golden = json.load(f)["entities"]
    table = {e["key"]: e for e in get(hub, "/api/integration/manifest")[1]["entities"]}
    assert {(p, "0000TEST0000001_" + k) for k, e in table.items() for p in [e["platform"]]} == {(p, u) for p, u, _ in golden}
    for platform, unique_id, entity_id in golden:
        e = table[unique_id.split("_", 1)[1]]
        # HA builds the entity id from the printer's device name and the entity name
        assert entity_id == "%s.my_h2s_%s" % (platform, slug(e["name"])), e


def test_entity_descriptors_say_where_state_lives_and_what_buttons_do(hub):
    table = {e["key"]: e for e in get(hub, "/api/integration/manifest")[1]["entities"]}
    assert [k for k, e in table.items() if e.get("state") == "local"] == ["print_file", "timelapse", "bed_leveling"]
    assert table["timelapse"]["default"] is False and table["bed_leveling"]["default"] is True
    press = table["start_print"]["press"]
    assert press["path"] == "/api/actions/print_file" and press["needs"] == {"print_file": "Select a print file first"}
    assert press["body"] == {"serial": "$serial", "file": "$print_file", "timelapse": "$timelapse",
                             "bed_leveling": "$bed_leveling", "use_ams": True}
    assert table["reread_slot_3"]["press"] == {"path": "/api/integration/printers/$serial/rfid", "body": {"ams_id": 0, "slot_id": 2}}
    assert table["reread_slot_1"]["icon"] == "mdi:nfc-search-variant" and "button" == table["reread_slot_4"]["platform"]


def test_the_revision_covers_the_entities_and_the_api_version_stays_1(hub, monkeypatch):
    first = get(hub, "/api/integration/manifest")[1]
    assert first["api_version"] == 1
    monkeypatch.setattr(actions, "ENTITIES", [dict(e, name=e["name"] + "!") for e in actions.ENTITIES])
    assert get(hub, "/api/integration/manifest")[1]["revision"] != first["revision"]


# ---- state
def test_state_is_internal_only(hub):
    assert hub.req("GET", "/api/integration/state", user=ADMIN)[0] == 403
    assert hub.req("GET", "/api/integration/state")[0] == 401


def test_state_lists_the_bridge_printers_with_the_revision(hub):
    body = state(hub)
    assert body["manifest_revision"] == actions.manifest_revision()
    assert sorted(body["printers"]) == [SERIAL, SERIAL2]
    p = body["printers"][SERIAL]
    assert (p["name"], p["model"]) == ("Lab", "H2S")
    assert set(p["entities"]) == {"print_file", "cloud_sender", "cloud_camera", "current_print", "chamber_light", "queue"}


def test_the_cloud_sender_follows_the_print_result(hub):
    assert me(hub)["cloud_sender"] == {"state": "idle", "attributes": {
        "account_email": "a@example.invalid", "last_file": None, "last_time": None, "last_error": None}}
    make_3mf(hub.media / "druckwerk" / "b.gcode.3mf")
    hub.req("POST", "/api/actions/print_file", {"serial": SERIAL, "file": "b.gcode.3mf"}, secret=True)
    sender = me(hub)["cloud_sender"]
    assert sender["state"] == "sent" and sender["attributes"]["last_file"] == "b.gcode.3mf"
    FakeBridge.post_answer = {"/api/print": (502, {"error": "no"})}
    hub.req("POST", "/api/actions/print_file", {"serial": SERIAL, "file": "b.gcode.3mf"}, secret=True)
    sender = me(hub)["cloud_sender"]
    assert sender["state"] == "error" and sender["attributes"]["last_error"] == "no"
    assert me(hub, SERIAL2)["cloud_sender"]["state"] == "idle" and me(hub, SERIAL2)["cloud_sender"]["attributes"]["account_email"] is None


# ---- files
def test_the_file_list_has_subfolders_sorted_and_only_sliced_files(hub):
    base = hub.media / "druckwerk"
    (base / "sub").mkdir()
    for name in ("b.gcode.3mf", "sub/a.GCODE.3MF", "a.gcode.3mf"):
        make_3mf(base / name)
    (base / "model.stl").write_bytes(b"x")
    assert get(hub, "/api/files") == (200, {"files": ["a.gcode.3mf", "b.gcode.3mf", "sub/a.GCODE.3MF"]})
    assert me(hub)["print_file"] == {"options": ["a.gcode.3mf", "b.gcode.3mf", "sub/a.GCODE.3MF"]}
    assert hub.req("GET", "/api/files", user=ADMIN)[0] == 403


# ---- upload
def zip_bytes(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in members.items():
            z.writestr(name, text)
    return out.getvalue()


def good_zip(gcode="G1 X1 E1\n"):
    return zip_bytes({"Metadata/plate_2.gcode": gcode})


def upload(hub, name, data, query="", secret=True):
    return hub.req("PUT", "/api/upload/%s%s" % (name, query), raw=data, secret=secret)


def test_upload_stores_the_file_and_answers_name_size_and_hash(hub):
    import hashlib
    data = good_zip()
    status, body = upload(hub, "new one.gcode.3mf".replace(" ", "%20"), data)
    assert status == 200 and body == {"file": "new one.gcode.3mf", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    dest = hub.media / "druckwerk" / "new one.gcode.3mf"
    assert dest.read_bytes() == data and oct(dest.stat().st_mode & 0o777) == "0o644"
    assert [p.name for p in (hub.media / "druckwerk").iterdir()] == ["new one.gcode.3mf"]  # no .part left
    assert "new one.gcode.3mf" in get(hub, "/api/files")[1]["files"]


@pytest.mark.parametrize("data,text", [
    (b"not a zip", "Body is not a 3MF (zip) file."),
    (zip_bytes({"Metadata/other.txt": "x"}), "The 3MF has no sliced plate (Metadata/plate_N.gcode); export it as .gcode.3mf."),
])
def test_upload_refuses_a_body_that_is_not_a_sliced_3mf(hub, data, text):
    status, body = upload(hub, "x.gcode.3mf", data)
    assert (status, body["error"]) == (400, text)
    assert list((hub.media / "druckwerk").iterdir()) == []


@pytest.mark.parametrize("name", ["x.stl", ".hidden.gcode.3mf", "a%2Fb.gcode.3mf", "a" * 120 + ".gcode.3mf", "x.gcode.3mf.exe"])
def test_upload_refuses_a_bad_name(hub, name):
    status, body = upload(hub, name, good_zip())
    assert status in (400, 404) and (status == 404 or body["error"] == "Name must be letters, digits, '._ -' and end in .gcode.3mf.")
    assert list((hub.media / "druckwerk").iterdir()) == []


def test_upload_limit_is_64_mb(hub, monkeypatch):
    assert files.MAX_UPLOAD == 64 * 1024 * 1024
    monkeypatch.setattr(files, "MAX_UPLOAD", 100)
    status, body = upload(hub, "x.gcode.3mf", b"x" * 101)
    assert (status, body["error"]) == (413, "File too large (limit 100 bytes).")
    assert upload(hub, "x.gcode.3mf", b"")[0] == 411


def test_upload_does_not_replace_a_file_unless_asked(hub):
    first = good_zip()
    second = good_zip("G1 X2 E1\n")
    assert upload(hub, "x.gcode.3mf", first)[0] == 200
    status, body = upload(hub, "x.gcode.3mf", second)
    assert (status, body["error"]) == (409, "File exists; add ?overwrite=1 to replace it.")
    assert (hub.media / "druckwerk" / "x.gcode.3mf").read_bytes() == first
    assert upload(hub, "x.gcode.3mf", second, "?overwrite=1")[0] == 200
    assert (hub.media / "druckwerk" / "x.gcode.3mf").read_bytes() == second


def test_upload_is_internal_only(hub):
    assert upload(hub, "x.gcode.3mf", good_zip(), secret=False)[0] == 401
    assert hub.req("PUT", "/api/upload/x.gcode.3mf", raw=good_zip(), user=USER)[0] == 403


# ---- camera
def test_camera_state_online_offline_and_transcoding(hub):
    FakeBridge.printer_extra = {SERIAL: {"live": {"cached": True}}, SERIAL2: {"live": None}}
    assert me(hub)["cloud_camera"] == {"online": True, "stream": "/api/camera/%s/live" % SERIAL, "transcode": False,
                                       "snapshot": "/api/integration/printers/%s/snapshot" % SERIAL}
    offline = me(hub, SERIAL2)["cloud_camera"]
    assert offline["online"] is False and offline["stream"] is None and offline["transcode"] is True  # a P1S sends JPEG


def test_snapshot_is_proxied_while_online_and_a_placeholder_while_offline(hub):
    FakeBridge.printer_extra = {SERIAL: {"live": {"cached": True}}}
    status, body = get(hub, "/api/integration/printers/%s/snapshot" % SERIAL)
    assert status == 200 and body == b"FRAME:/api/camera/%s/snapshot" % SERIAL.encode()
    status, body = get(hub, "/api/integration/printers/%s/snapshot" % SERIAL2)
    with open(os.path.join(REPO, "hub", "offline.jpg"), "rb") as f:
        assert (status, body) == (200, f.read())
    assert get(hub, "/api/integration/printers/NOPE/snapshot")[0] == 404
    assert hub.req("GET", "/api/integration/printers/%s/snapshot" % SERIAL, user=ADMIN)[0] == 403


# ---- chamber light
def with_light(hub, value):
    hub.ha.entities = ENTITIES + [{"entity_id": "light.lab_h2s_chamber_light", "device_id": "dev1", "platform": VENDOR,
                                   "unique_id": "H2S_%s_chamber_light" % SERIAL}]
    hub.ha.s["light.lab_h2s_chamber_light"] = st(value)


def test_the_light_mirrors_the_vendor_light(hub):
    assert me(hub)["chamber_light"] == {"on": None}  # no vendor light
    for value, want in (("on", True), ("off", False), ("unavailable", None)):
        with_light(hub, value)
        hub.app._cache.clear()
        assert me(hub)["chamber_light"] == {"on": want}
    assert me(hub, SERIAL2)["chamber_light"] == {"on": None}


def test_the_light_route_switches_it_through_the_bridge(hub):
    status, _ = hub.req("POST", "/api/integration/printers/%s/light" % SERIAL, {"on": True}, secret=True)
    assert status == 200 and (("/api/light/" + SERIAL), {"on": True}) in FakeBridge.log
    assert hub.req("POST", "/api/integration/printers/%s/light" % SERIAL, {"on": "yes"}, secret=True)[0] == 400
    assert hub.req("POST", "/api/integration/printers/%s/light" % SERIAL, {"on": True}, user=ADMIN)[0] == 403
    FakeBridge.post_answer = {"/api/light/" + SERIAL: (404, {"error": "unknown printer"})}
    assert hub.req("POST", "/api/integration/printers/%s/light" % SERIAL, {"on": False}, secret=True) == (404, {"error": "unknown printer"})


# ---- re-read slot
def test_the_rfid_route_asks_the_bridge_to_read_the_slot_again(hub):
    status, _ = hub.req("POST", "/api/integration/printers/%s/rfid" % SERIAL, {"ams_id": 0, "slot_id": 2}, secret=True)
    assert status == 200 and ("/api/rfid/" + SERIAL, {"ams_id": 0, "slot_id": 2}) in FakeBridge.log
    hub.req("POST", "/api/integration/printers/%s/rfid" % SERIAL, {"slot_id": 1}, secret=True)
    assert ("/api/rfid/" + SERIAL, {"ams_id": 0, "slot_id": 1}) in FakeBridge.log  # ams_id defaults to 0
    assert hub.req("POST", "/api/integration/printers/%s/rfid" % SERIAL, {"slot_id": 0}, user=ADMIN)[0] == 403
    FakeBridge.post_answer = {"/api/rfid/" + SERIAL: (400, {"error": "Body must be {\"ams_id\": 0-7, \"slot_id\": 0-3}."})}
    assert hub.req("POST", "/api/integration/printers/%s/rfid" % SERIAL, {"slot_id": 9}, secret=True)[0] == 400


# ---- current print picture
def with_task(hub, name):
    hub.ha.entities = hub.ha.entities + [{"entity_id": "sensor.lab_h2s_subtask_name", "device_id": "dev1", "platform": VENDOR,
                                          "unique_id": SERIAL + "_subtask_name"}]
    hub.ha.s["sensor.lab_h2s_subtask_name"] = st(name)


def test_the_picture_follows_the_last_sent_file_and_then_the_printers_task(hub):
    base = hub.media / "druckwerk"
    assert me(hub)["current_print"] == {"token": None, "file_path": None, "has_preview": False}
    assert get(hub, "/api/integration/printers/%s/image" % SERIAL)[0] == 404
    sent = make_3mf(base / "sent.gcode.3mf")
    hub.req("POST", "/api/actions/print_file", {"serial": SERIAL, "file": "sent.gcode.3mf"}, secret=True)
    cur = me(hub)["current_print"]
    assert cur["file_path"] == sent and cur["has_preview"] and len(cur["token"]) == 12
    assert get(hub, "/api/integration/printers/%s/image" % SERIAL) == (200, b"\x89PNG\r\n\x1a\nfake")
    # the printer reports another job that exists as a file: it wins, top-level copy first
    (base / "sub").mkdir()
    make_3mf(base / "other.gcode.3mf")
    make_3mf(base / "sub" / "other.gcode.3mf")
    with_task(hub, "other")
    hub.app._cache.clear()
    cur2 = me(hub)["current_print"]
    assert cur2["file_path"] == str(base / "other.gcode.3mf") and cur2["token"] != cur["token"]
    # an unknown task name has no file; an unknown task state falls back to the last sent file
    hub.ha.s["sensor.lab_h2s_subtask_name"] = st("nothing here")
    assert me(hub)["current_print"] == {"token": None, "file_path": None, "has_preview": False}
    hub.ha.s["sensor.lab_h2s_subtask_name"] = st("unavailable")
    assert me(hub)["current_print"]["file_path"] == sent


def test_a_file_without_a_slicer_picture_is_drawn_from_its_gcode(hub):
    path = hub.media / "druckwerk" / "plain.gcode.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/plate_1.gcode", "M83\nG1 X10 Y10 F3000\n; CHANGE_LAYER\nG1 X10 Y10 E0\nG1 X60 Y10 E2\n")
    with_task(hub, "plain")  # the printer reports this job
    assert me(hub)["current_print"]["has_preview"] is True
    assert get(hub, "/api/integration/printers/%s/image" % SERIAL)[1][:4] == b"\x89PNG"


# ---- queue as a todo list
def test_the_queue_items_are_the_waiting_jobs_of_the_printer(hub):
    hub.grant(USER, SERIAL, needs_approval=True)
    a = hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL, "name": "First"}, user=ADMIN)[1]
    anywhere = hub.req("POST", "/api/queue", {"file": "b.gcode.3mf", "target": "any", "model": "H2S"}, user=ADMIN)[1]
    pending = hub.req("POST", "/api/queue", {"file": "c.gcode.3mf", "target": SERIAL}, user=USER)[1]
    dry = hub.req("POST", "/api/queue", {"kind": "dry", "target": SERIAL, "ams_id": 0, "temp": 55, "hours": 4,
                                         "filament": "PETG"}, user=ADMIN)[1]
    hub.req("POST", "/api/queue", {"file": "d.gcode.3mf", "target": SERIAL2}, user=ADMIN)
    items = me(hub)["queue"]["items"]
    assert [i["uid"] for i in items] == [a["id"], pending["id"], dry["id"]]  # not the "any" job, not the other printer
    assert items[0] == {"uid": a["id"], "summary": "First", "description": "File: a.gcode.3mf\nOwner: U-ADMIN"}
    assert items[1]["description"] == "File: c.gcode.3mf\nOwner: U-USER (needs approval)"
    assert items[2]["description"] == "Dry AMS 1, PETG, 55 C, 4 h\nOwner: U-ADMIN"
    assert anywhere["id"] not in [i["uid"] for i in items]
    assert [i["summary"] for i in me(hub, SERIAL2)["queue"]["items"]] == ["d"]
