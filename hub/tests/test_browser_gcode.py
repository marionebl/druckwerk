"""POST /api/jobs/{id}/gcode (G-code sliced in the browser) goes through identity, the grant check and the owner rule."""
import json
import os
import zipfile

import pytest

from hubfakes import ADMIN, OTHER, SERIAL, USER

TRAY = {"ams": "0", "id": "0", "type": "PETG", "idx": "GFG02", "name": "PETG HF", "colour": "#ADB1B2"}
PRESETS = {"model": "H2S", "machine": {"name": "Bambu Lab H2S 0.4 nozzle"}, "process": {q: {"name": q} for q in ("good", "better", "best")},
           "filaments": [{"name": "Generic PETG", "id": "GFG99", "type": "PETG", "n": 9, "config": {}}]}
GCODE = b"; CHANGE_LAYER\nG1 X1 Y1 E1\n"
QS = "serial=%s&quality=better&tray=0:0&bed=High%%20Temp%%20Plate&filament=Generic%%20PETG&seconds=600&grams=4.2&metres=1.4&engine=mt&slice_s=9" % SERIAL


@pytest.fixture
def web(hub, monkeypatch):
    (hub.tmp / "engine" / "presets").mkdir(parents=True)
    (hub.tmp / "engine" / "presets" / "H2S.json").write_text(json.dumps(PRESETS))
    monkeypatch.setattr(hub.s, "ENGINE", str(hub.tmp / "engine"))
    monkeypatch.setattr(hub.s, "printers", lambda: [{"serial": SERIAL, "name": "Lab", "model": "H2S", "bed": "High Temp Plate",
                                                      "qualities": ["good", "better", "best"], "trays": [TRAY], "online": True}])
    hub.setup_printers()
    return hub


def upload(hub, user=USER):
    return hub.req("POST", "/api/jobs?name=cube.stl", raw=b"solid x", user=user)[1]["id"]


def test_gcode_needs_identity(web):
    jid = upload(web)
    assert web.req("POST", "/api/jobs/%s/gcode?%s" % (jid, QS), raw=GCODE)[0] == 401


def test_user_without_a_grant_cannot_create_a_browser_slice(web):
    jid = upload(web)
    st, body = web.req("POST", "/api/jobs/%s/gcode?%s" % (jid, QS), raw=GCODE, user=USER)
    assert st == 403 and body["error"] == "rule: slicing needs an active grant on the printer"
    assert web.s.jobs[jid]["state"] == "uploaded" and not os.path.exists(os.path.join(web.s.JOBS, jid, "out"))
    assert web.req("GET", "/api/engine/filament?serial=%s&tray=0:0" % SERIAL, user=USER)[0] == 403


def test_slices_belong_to_the_person_who_uploaded_them(web):
    jid = upload(web)
    web.grant(OTHER)
    st, body = web.req("POST", "/api/jobs/%s/gcode?%s" % (jid, QS), raw=GCODE, user=OTHER)
    assert st == 403 and body["error"].startswith("rule: slices belong")


def test_granted_user_slices_and_queues_a_browser_slice(web):
    web.grant(USER)
    jid = upload(web)
    st, job = web.req("POST", "/api/jobs/%s/gcode?%s" % (jid, QS), raw=GCODE, user=USER)
    assert st == 200 and job["state"] == "done" and job["info"]["has_png"] is False and job["owner_user_id"] == USER
    assert web.req("GET", "/api/jobs/%s/plate.gcode" % jid, user=USER)[1] == GCODE
    st, got = web.req("GET", "/api/jobs/" + jid, user=USER)
    assert st == 200 and got["state"] == "done"
    st, queued = web.req("POST", "/api/jobs/%s/queue" % jid, {"target": SERIAL}, user=USER)  # no plate image: still queues
    assert st == 200, queued
    assert queued["grams"] == 4.2 and queued["print_seconds"] == 600 and queued["plate_png"] is None
    with zipfile.ZipFile(os.path.join(web.s.SHARE, queued["file"])) as z:
        assert z.read("Metadata/plate_1.gcode") == GCODE
    assert web.req("GET", "/api/engine/filament?serial=%s&tray=0:0" % SERIAL, user=USER)[1] == {"filament": "Generic PETG"}


def test_global_admin_slices_their_own_upload(web):
    jid = upload(web, ADMIN)
    assert web.req("POST", "/api/jobs/%s/gcode?%s" % (jid, QS), raw=GCODE, user=ADMIN)[0] == 200
