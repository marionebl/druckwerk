"""Print options, job names, queue files by absolute path, and AMS drying through the real server."""
import os
import zipfile

from hubfakes import ADMIN, SERIAL, SERIAL2, USER, FakeBridge, make_3mf

DRY = {"ams_id": 0, "temp": 55, "hours": 8, "filament": "PLA"}


def unit(**kw):
    return dict({"ams_id": 0, "name": "AMS 2 Pro", "status": "off", "active": False, "left_min": 0, "filament": "",
                 "temp": None, "hours": None, "temp_min": 45, "temp_max": 65}, **kw)


# ---- print options
def test_queue_takes_print_options_and_the_send_passes_them(hub):
    hub.setup_printers()
    hub.seed_share()
    hub.set_print(gcode_state="IDLE")
    opts = {"bed_leveling": "auto", "flow_cali": False, "layer_inspect": True, "bed_type": "textured_plate"}
    st, job = hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL, "options": opts}, user=ADMIN)
    assert st == 200 and job["options"]["bed_leveling"] == "auto" and job["options"]["vibration_cali"] is True
    assert hub.req("POST", "/api/printers/%s/start" % SERIAL, {"plate_clear": True, "job_id": job["id"]}, user=ADMIN)[0] == 200
    assert hub.sent() == [{"serial": SERIAL, "filename": "a.gcode.3mf", "file_b64": "...", "timelapse": False,
                           "bed_leveling": "auto", "use_ams": True, "flow_cali": False, "layer_inspect": True,
                           "bed_type": "textured_plate"}]


def test_bad_print_options_are_a_400(hub):
    hub.setup_printers()
    for bad in ({"bed_leveling": "maybe"}, {"bed_type": "gold"}, {"unknown": True}):
        st, body = hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL, "options": bad}, user=ADMIN)
        assert st == 400 and "bad option" in body["error"]


# ---- names
def test_name_is_stored_and_defaults_to_the_file(hub):
    hub.setup_printers()
    named = hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL, "name": "Benchy for Anna"}, user=ADMIN)[1]
    plain = hub.req("POST", "/api/queue", {"file": "b.gcode.3mf", "target": SERIAL}, user=ADMIN)[1]
    assert (named["name"], plain["name"]) == ("Benchy for Anna", "b")
    assert hub.req("POST", "/api/queue", {"file": "c.gcode.3mf", "target": SERIAL, "name": "x" * 81}, user=ADMIN)[0] == 400


def test_name_survives_slice_to_queue(hub):
    hub.setup_printers()
    jid = hub.sliced(owner=ADMIN)
    job = hub.req("POST", "/api/jobs/%s/queue" % jid, {"target": SERIAL, "name": "My cube"}, user=ADMIN)[1]
    assert job["name"] == "My cube" and job["options"]["flow_cali"] is True


def test_started_event_carries_the_name(hub):
    hub.setup_printers()
    hub.seed_share()
    hub.set_print(gcode_state="IDLE")
    since = hub.req("GET", "/api/events", secret=True)[1]["seq"]
    job = hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL, "name": "Live name"}, user=ADMIN)[1]
    hub.req("POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user=ADMIN)
    ev = hub.req("GET", "/api/events?since=%d" % since, secret=True)[1]["events"]
    started = [e for e in ev if e["type"] == "job_started"]
    assert started and started[0]["job"]["name"] == "Live name" and started[0]["job"]["id"] == job["id"]


# ---- files by absolute path
def roots(hub, tmp_path):
    hub.app.media_roots = [os.path.realpath(str(hub.media))]
    return hub.media


def test_a_sliced_file_under_media_is_copied_into_the_share_and_queued(hub, tmp_path):
    hub.setup_printers()
    media = roots(hub, tmp_path)
    src = make_3mf(media / "plate_v4.gcode.3mf")
    st, job = hub.req("POST", "/api/queue", {"file": src, "target": SERIAL}, user=ADMIN)
    assert st == 200 and job["file"] != "plate_v4.gcode.3mf" and job["file"].startswith("plate_v4_") and job["file"].endswith(".gcode.3mf")
    assert job["name"] == "plate_v4" and job["source"] == "plate_v4.gcode.3mf"
    with open(os.path.join(hub.s.SHARE, job["file"]), "rb") as f, open(src, "rb") as g:
        assert f.read() == g.read()
    assert job["grams"] is not None and job["filaments"] and job["plate_png"].endswith("/plate.png")
    assert os.path.exists(src)  # the original stays


def test_paths_outside_media_and_share_are_refused(hub, tmp_path):
    hub.setup_printers()
    media = roots(hub, tmp_path)
    outside = make_3mf(tmp_path / "outside.gcode.3mf")
    escape = str(media) + "/../outside.gcode.3mf"
    link = media / "link.gcode.3mf"
    os.symlink(outside, link)
    for bad in (outside, escape, str(link), "/etc/passwd.gcode.3mf", str(media / "missing.gcode.3mf"), "../a.gcode.3mf"):
        st, body = hub.req("POST", "/api/queue", {"file": bad, "target": SERIAL}, user=ADMIN)
        assert st == 400, bad
    assert hub.req("GET", "/api/queue", user=ADMIN)[1]["jobs"] == []
    assert not os.path.exists(hub.s.SHARE) or os.listdir(hub.s.SHARE) == []


def test_a_3mf_that_is_not_sliced_is_refused(hub, tmp_path):
    hub.setup_printers()
    media = roots(hub, tmp_path)
    plain = media / "model.gcode.3mf"
    with zipfile.ZipFile(plain, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
    st, body = hub.req("POST", "/api/queue", {"file": str(plain), "target": SERIAL}, user=ADMIN)
    assert st == 400 and "sliced" in body["error"]
    notzip = media / "x.gcode.3mf"
    notzip.write_bytes(b"not a zip")
    assert hub.req("POST", "/api/queue", {"file": str(notzip), "target": SERIAL}, user=ADMIN)[0] == 400
    assert os.listdir(hub.s.SHARE) == [] if os.path.exists(hub.s.SHARE) else True


def test_path_adds_still_need_a_grant(hub, tmp_path):
    hub.setup_printers()
    media = roots(hub, tmp_path)
    src = make_3mf(media / "p.gcode.3mf")
    st, body = hub.req("POST", "/api/queue", {"file": src, "target": SERIAL}, user=USER)
    assert st == 403 and (not os.path.exists(hub.s.SHARE) or os.listdir(hub.s.SHARE) == [])


# ---- drying
def test_printer_view_lists_drying_units(hub):
    hub.setup_printers()
    FakeBridge.drying = [unit(status="drying", active=True, left_min=90, filament="PLA", temp=45, hours=12)]
    p = hub.req("GET", "/api/printers", user=ADMIN)[1][0]
    assert p["drying"] == FakeBridge.drying
    hub.grant(USER, SERIAL)
    assert hub.req("GET", "/api/printers", user=USER)[1][0]["drying"] == FakeBridge.drying  # users may look


def test_admin_starts_drying_after_confirming(hub):
    hub.setup_printers()
    hub.set_print(gcode_state="IDLE")
    path = "/api/printers/%s/drying" % SERIAL
    assert hub.req("POST", path, DRY, user=ADMIN)[0] == 409  # not confirmed
    assert FakeBridge.log == []
    st, job = hub.req("POST", path, dict(DRY, confirmed=True), user=ADMIN)
    assert st == 200 and job["kind"] == "dry" and job["state"] == "drying"
    assert FakeBridge.log == [("/api/ams/%s/drying" % SERIAL, {"ams_id": 0, "temp": 55, "hours": 8, "filament": "PLA"})]
    snap = hub.req("GET", "/api/queue", user=ADMIN)[1]
    assert [j["state"] for j in snap["jobs"]] == ["drying"]


def test_non_admin_cannot_start_or_stop_drying(hub):
    hub.setup_printers()
    hub.set_print(gcode_state="IDLE")
    hub.grant(USER, SERIAL)
    st, body = hub.req("POST", "/api/printers/%s/drying" % SERIAL, dict(DRY, confirmed=True), user=USER)
    assert st == 403 and body["error"].startswith("rule:")
    assert hub.req("POST", "/api/printers/%s/drying/stop" % SERIAL, {"ams_id": 0}, user=USER)[0] == 403
    assert hub.req("POST", "/api/printers/%s/drying" % SERIAL, dict(DRY, confirmed=True))[0] == 401
    assert hub.req("POST", "/api/printers/%s/drying" % SERIAL2, dict(DRY, confirmed=True), user=USER)[0] == 403
    assert FakeBridge.log == []


def test_drying_ranges_are_a_400_and_the_bridge_refusal_is_passed_on(hub):
    hub.setup_printers()
    hub.set_print(gcode_state="IDLE")
    path = "/api/printers/%s/drying" % SERIAL
    for bad in ({"temp": 30}, {"temp": 120}, {"hours": 0}, {"hours": 100}, {"ams_id": 9}, {"temp": "hot"}):
        assert hub.req("POST", path, dict(DRY, confirmed=True, **bad), user=ADMIN)[0] == 400, bad
    assert FakeBridge.log == []
    FakeBridge.drying_fail["/api/ams/%s/drying" % SERIAL] = (400, "temp must be 45 to 65 for AMS 2 Pro")
    st, body = hub.req("POST", path, dict(DRY, confirmed=True, temp=80), user=ADMIN)
    assert st == 400 and "45 to 65" in body["error"]
    # A refused start leaves no job behind.
    assert hub.req("GET", "/api/queue", user=ADMIN)[1]["jobs"] == []


def test_direct_start_follows_the_busy_rule(hub):
    hub.setup_printers()  # the fake printer is running
    st, body = hub.req("POST", "/api/printers/%s/drying" % SERIAL, dict(DRY, confirmed=True), user=ADMIN)
    assert st == 409 and "not ready" in body["error"]
    assert FakeBridge.log == [] and hub.req("GET", "/api/queue", user=ADMIN)[1]["jobs"] == []


def test_stop_drying_stops_the_job_and_the_ams(hub):
    hub.setup_printers()
    hub.set_print(gcode_state="IDLE")
    job = hub.req("POST", "/api/printers/%s/drying" % SERIAL, dict(DRY, confirmed=True), user=ADMIN)[1]
    assert hub.req("POST", "/api/printers/%s/drying/stop" % SERIAL, {"ams_id": 0}, user=ADMIN)[0] == 200
    assert FakeBridge.log[-1] == ("/api/ams/%s/drying/stop" % SERIAL, {"ams_id": 0})
    snap = hub.req("GET", "/api/queue", user=ADMIN)[1]
    assert snap["jobs"] == [] and (snap["history"][0]["id"], snap["history"][0]["state"]) == (job["id"], "cancelled")
    # Drying that was not started through the queue stops too.
    assert hub.req("POST", "/api/printers/%s/drying/stop" % SERIAL, {"ams_id": 128}, user=ADMIN)[0] == 200
    assert FakeBridge.log[-1] == ("/api/ams/%s/drying/stop" % SERIAL, {"ams_id": 128})


def test_queue_routes_for_dry_jobs(hub):
    hub.setup_printers()
    hub.set_print(gcode_state="IDLE")
    hub.grant(USER, SERIAL, needs_approval=True)
    st, job = hub.req("POST", "/api/queue", dict(DRY, kind="dry", target=SERIAL), user=USER)
    assert st == 200 and job["kind"] == "dry" and job["state"] == "pending_approval"
    assert hub.req("POST", "/api/queue/%s/approve" % job["id"], {}, user=ADMIN)[1]["state"] == "queued"
    assert hub.req("POST", "/api/queue", dict(DRY, kind="dry", target="any"), user=ADMIN)[0] == 400
    st, started = hub.req("POST", "/api/printers/%s/start" % SERIAL, {"job_id": job["id"], "confirmed": True}, user=ADMIN)
    assert st == 200 and started["state"] == "drying"
    assert hub.req("POST", "/api/queue/%s/stop" % job["id"], {}, user=USER)[0] == 403
    assert hub.req("POST", "/api/queue/%s/stop" % job["id"], {}, user=ADMIN)[0] == 200
    assert FakeBridge.log[-1][0] == "/api/ams/%s/drying/stop" % SERIAL


def test_dry_jobs_send_no_job_events_and_do_not_settle_spools(hub):
    hub.setup_printers()
    hub.set_print(gcode_state="IDLE")
    since = hub.req("GET", "/api/events", secret=True)[1]["seq"]
    hub.req("POST", "/api/printers/%s/drying" % SERIAL, dict(DRY, confirmed=True), user=ADMIN)
    hub.req("POST", "/api/printers/%s/drying/stop" % SERIAL, {"ams_id": 0}, user=ADMIN)
    types = [e["type"] for e in hub.req("GET", "/api/events?since=%d" % since, secret=True)[1]["events"]]
    assert types and "job_started" not in types and "job_done" not in types and "job_failed" not in types
