"""GET /api/printers, camera and cover proxies."""
from hubfakes import ADMIN, OTHER, SERIAL, SERIAL2, USER, FakeBridge


def test_printer_view_from_the_bridge_status(hub):
    hub.setup_printers()
    st, plist = hub.req("GET", "/api/printers", user=ADMIN)
    assert st == 200 and [p["serial"] for p in plist] == [SERIAL, SERIAL2]
    p = plist[0]
    assert (p["name"], p["model"], p["role"], p["online"], p["status"], p["stage"]) == ("Lab", "H2S", "admin", True, "running", "printing")
    assert (p["progress"], p["remaining_min"], p["layer"], p["total_layers"]) == (42.0, 73, 10, 50)
    assert (p["nozzle_temp"], p["nozzle_target"], p["bed_temp"], p["bed_target"], p["chamber_temp"]) == (220, 220, 60, 60, 30)
    assert p["task_name"] == "cube" and p["end_time"] == "1970-01-01T01:29:00+00:00"  # the fake clock stands at 1000 s
    assert p["active_tray"] == {"unit": 0, "slot": 1}
    assert p["hms"] == [{"code": "HMS_0300_0100_0002_0007", "severity": "serious", "text": "", "url": "https://wiki.bambulab.com/en/hms/home"}]
    assert p["camera_url"] == "api/printers/%s/camera" % SERIAL and p["cover_url"] == "api/printers/%s/cover" % SERIAL
    assert p["ams"] == [{"unit": 0, "humidity_index": 4, "temp": 28.5, "trays": [
        {"slot": 0, "type": "PETG", "colour": "ADB1B2", "name": "PETG HF", "remain_pct": 80, "spool_id": None},
        {"slot": 1, "type": "PLA", "colour": "BB3D43", "name": "PLA Basic", "remain_pct": 55, "spool_id": None}]}]
    assert p["current_job"] is None and p["queue_length"] == 0 and p["external"] is None
    other = plist[1]
    assert (other["status"], other["stage"], other["task_name"], other["cover_url"], other["ams"]) == ("idle", "idle", None, None, [])


def test_a_printer_the_bridge_cannot_read_is_offline_with_empty_values(hub):
    hub.setup_printers()
    FakeBridge.status_fail = {SERIAL}
    p = hub.req("GET", "/api/printers", user=ADMIN)[1][0]
    assert (p["online"], p["status"], p["progress"], p["task_name"], p["ams"], p["hms"], p["active_tray"]) == (False, None, None, None, [], [], None)


def test_tray_gets_spool_id(hub):
    hub.setup_printers()
    spool = hub.cloud_spool(type="PETG", colour="ADB1B2", grams_left=900, printer=SERIAL, unit=0, slot=0)
    hub.req("GET", "/api/spools", user=ADMIN)  # the printer list only reads a library that was fetched
    tray = hub.req("GET", "/api/printers", user=ADMIN)[1][0]["ams"][0]["trays"][0]
    assert tray["spool_id"] == spool["id"]


def test_printers_filtered_by_active_grant(hub):
    hub.setup_printers()
    assert hub.req("GET", "/api/printers", user=USER)[1] == []
    hub.grant(USER, SERIAL)
    st, plist = hub.req("GET", "/api/printers", user=USER)
    assert [(p["serial"], p["role"]) for p in plist] == [(SERIAL, "user")]
    hub.grant(OTHER, SERIAL, condition_entity="schedule.workday")
    assert len(hub.req("GET", "/api/printers", user=OTHER)[1]) == 1
    hub.ha.s["schedule.workday"]["state"] = "off"  # HA decides: the grant lapses
    assert hub.req("GET", "/api/printers", user=OTHER)[1] == []


def test_camera_and_cover_proxies_need_a_grant(hub):
    hub.setup_printers()
    assert hub.req("GET", "/api/printers/%s/camera" % SERIAL, user=USER)[0] == 403
    hub.grant(USER, SERIAL)
    st, body = hub.req("GET", "/api/printers/%s/camera?protocols=rtsp" % SERIAL, user=USER)
    assert st == 200 and body == b"FRAME:/api/camera/%s/live?protocols=rtsp" % SERIAL.encode()
    assert hub.req("GET", "/api/printers/%s/camera?mode=snapshot" % SERIAL, user=USER)[1].endswith(b"/snapshot")
    assert hub.req("GET", "/api/printers/%s/cover" % SERIAL, user=OTHER)[0] == 403
    assert hub.req("GET", "/api/printers/NOPE/camera", user=USER)[0] == 404


def test_printer_light_is_for_admins_of_the_printer(hub):
    hub.setup_printers()
    url = "/api/printers/%s/light" % SERIAL
    assert hub.req("GET", "/api/printers", user=ADMIN)[1][0]["light"] is True  # lights_report: chamber_light on
    assert hub.req("POST", url, {"on": False}, user=USER)[0] == 403  # no grant
    hub.grant(USER, SERIAL)
    assert hub.req("POST", url, {"on": False}, user=USER)[0] == 403  # a plain user
    assert hub.req("POST", url, {"on": "no"}, user=ADMIN)[0] == 400
    assert hub.req("POST", url, {"on": False}, user=ADMIN) == (200, {"on": False})
    assert ("/api/light/" + SERIAL, {"on": False}) in FakeBridge.log
    hub.owner(USER, "FAKE0002")  # owner of the other printer: no rights here
    assert hub.req("POST", url, {"on": True}, user=USER)[0] == 403
    hub.grant(OTHER, SERIAL)
    assert hub.req("POST", url, {"on": True}, user=OTHER)[0] == 403
    hub.owner(OTHER)  # the printer's owner is its admin
    assert hub.req("POST", url, {"on": True}, user=OTHER)[0] == 200
    hub.set_print(lights_report=[])
    assert hub.req("GET", "/api/printers", user=ADMIN)[1][0]["light"] is None
