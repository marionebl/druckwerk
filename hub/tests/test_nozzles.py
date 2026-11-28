"""Nozzles: the installed nozzle read from Home Assistant, the nozzle a job needs, and requests to change the nozzle."""
import json
import os

import pytest

from hubfakes import ADMIN, GCODE, OTHER, SERIAL, SERIAL2, USER, make_3mf, st

ACME = {"person.ada": "notify.mobile_app_ada", "person.cy": "mobile_app_cy"}
H2S = {"diameter": "0.4", "type": "hardened_steel"}


def install(hub, prefix="lab_h2s", size="0.4", type_="hardened_steel"):
    hub.ha.s["sensor.%s_nozzle_size" % prefix] = st(size, unit_of_measurement="mm")
    if type_ is not None:
        hub.ha.s["sensor.%s_nozzle_type" % prefix] = st(type_)


@pytest.fixture
def hub(hub):
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    hub.grant(OTHER, SERIAL)
    return hub


def printer(hub, serial=SERIAL, user=ADMIN):
    return next(p for p in hub.req("GET", "/api/printers", user=user)[1] if p["serial"] == serial)


# -- the installed nozzle is read live from the sensors, never stored --
def test_installed_nozzle_comes_from_the_ha_sensors(hub):
    assert printer(hub)["nozzle"] is None  # no sensors: unknown
    install(hub)
    install(hub, "lab_p1s", "0.40", "stainless_steel")  # "0.40" is the size 0.4
    assert printer(hub)["nozzle"] == H2S
    assert printer(hub, SERIAL2)["nozzle"] == {"diameter": "0.4", "type": "stainless_steel"}
    install(hub, size="0.6")  # swapped: the next read shows it
    assert printer(hub)["nozzle"] == {"diameter": "0.6", "type": "hardened_steel"}
    assert "nozzle" not in hub.app.registry.get(SERIAL)  # nothing of it is stored
    hub.ha.s["sensor.lab_h2s_nozzle_size"] = st("unavailable")
    assert printer(hub)["nozzle"] is None


def test_the_installed_nozzle_joins_the_library_on_read_only(hub):
    owned = [{"diameter": "0.6", "type": "hardened_steel"}]
    hub.app.registry.put(SERIAL, name="Lab", model="H2S", account="a_x", prefix="lab_h2s", nozzles=owned)
    install(hub)
    assert printer(hub)["nozzles"] == owned + [H2S]
    install(hub, size="0.6")
    assert printer(hub)["nozzles"] == owned  # already in the library
    assert hub.app.registry.get(SERIAL)["nozzles"] == owned


def test_setup_saves_the_library_and_rejects_bad_nozzles(hub):
    body = {"name": "Lab", "ha_prefix": "lab_h2s", "nozzles": [{"diameter": "0.4", "type": "hardened_steel"}, {"diameter": "0.6", "type": ""}]}
    st_, cfg = hub.req("PUT", "/api/setup/printers/" + SERIAL, body, user=ADMIN)
    assert st_ == 200 and cfg["nozzles"] == body["nozzles"]
    assert hub.req("PUT", "/api/setup/printers/" + SERIAL, dict(body, nozzles=[{"diameter": "0.5"}]), user=ADMIN)[0] == 400
    assert hub.req("PUT", "/api/setup/printers/" + SERIAL, dict(body, nozzles="0.4"), user=ADMIN)[0] == 400
    assert hub.req("PUT", "/api/setup/printers/" + SERIAL, {k: v for k, v in body.items() if k != "nozzles"}, user=ADMIN)[1]["nozzles"] == body["nozzles"]


# -- a job knows its nozzle; a start with another nozzle in the printer is refused --
def add_job(hub, nozzle):
    hub.seed_share()
    gcode = GCODE.replace("; EXECUTABLE", "; nozzle_diameter = %s\n; EXECUTABLE" % nozzle) if nozzle else GCODE
    make_3mf(os.path.join(hub.s.SHARE, "a.gcode.3mf"), gcode=gcode)
    return hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL}, user=ADMIN)[1]


def start(hub):
    hub.set_print(gcode_state="IDLE")
    return hub.req("POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user=ADMIN)


def test_a_job_takes_its_nozzle_from_the_gcode_header(hub):
    assert add_job(hub, "0.6")["nozzle"] == "0.6"
    assert add_job(hub, None)["nozzle"] is None  # the file does not say
    assert hub.req("GET", "/api/queue", user=ADMIN)[1]["jobs"][0]["nozzle"] == "0.6"


def test_start_is_refused_when_the_installed_nozzle_differs(hub):
    add_job(hub, "0.6")
    install(hub)
    status, body = start(hub)
    assert status == 409 and body["error"] == "This job needs a 0.6 mm nozzle; the printer has 0.4 mm."
    assert hub.sent() == [] and hub.req("GET", "/api/queue", user=ADMIN)[1]["jobs"][0]["state"] == "queued"


def test_start_is_allowed_when_the_nozzle_matches_or_is_unknown(hub):
    add_job(hub, "0.6")
    install(hub, size="0.60")
    assert start(hub)[0] == 200  # matches
    hub.app.queue.jobs.clear()
    add_job(hub, "0.6")
    hub.ha.s["sensor.lab_h2s_nozzle_size"] = st("unavailable")
    assert start(hub)[0] == 200  # unknown never blocks
    hub.app.queue.jobs.clear()
    add_job(hub, "0.6")
    del hub.ha.s["sensor.lab_h2s_nozzle_size"]
    assert start(hub)[0] == 200  # no sensor at all


def test_a_job_without_a_nozzle_starts_whatever_is_installed(hub):
    add_job(hub, None)
    install(hub, size="0.8")
    assert start(hub)[0] == 200


def test_a_slice_made_for_a_nozzle_carries_it_to_the_queue(hub):
    jid = hub.sliced()
    hub.s.jobs[jid]["nozzle"] = "0.6"
    hub.grant(USER, SERIAL)
    job = hub.req("POST", "/api/jobs/%s/queue" % jid, {"target": SERIAL}, user=USER)[1]
    assert job["nozzle"] == "0.6"


# -- nozzle requests --
def ask(hub, user=USER, serial=SERIAL, **body):
    if not hasattr(hub, "job"):
        hub.job = hub.queue_job(user=USER)  # a request belongs to a queue entry
    body = dict({"diameter": "0.6", "type": "hardened_steel", "job_id": hub.job["id"]}, **body)
    return hub.req("POST", "/api/printers/%s/nozzle-request" % serial, {k: v for k, v in body.items() if v is not None}, user=user)


def test_a_user_asks_and_the_request_is_listed(hub):
    install(hub)
    status, req = ask(hub, note="for the vase")
    assert status == 200 and (req["printer"], req["diameter"], req["type"], req["note"]) == (SERIAL, "0.6", "hardened_steel", "for the vase")
    assert req["user_id"] == USER and req["user_name"] == "U-USER" and req["job_id"] == hub.job["id"] and req["job_name"] == hub.job["name"]
    assert hub.req("GET", "/api/nozzle-requests", user=USER)[1] == [req]
    assert hub.req("GET", "/api/nozzle-requests", user=ADMIN)[1] == [req]
    with open(os.path.join(hub.tmp, "data", "nozzle_requests.json")) as f:
        assert json.load(f) == [req]


def test_validation(hub):
    install(hub)
    assert ask(hub, serial=SERIAL2)[0] == 403  # no grant there
    assert ask(hub, serial="NOPE")[0] == 404
    assert ask(hub, diameter="0.5")[0] == 400
    assert ask(hub, note="x" * 201)[0] == 400
    assert ask(hub, job_id="../x")[0] == 400 and ask(hub, job_id=None)[0] == 400  # a request needs a job
    assert ask(hub, job_id="0" * 12)[0] == 404
    assert ask(hub, user=OTHER)[0] == 403  # not their job
    assert ask(hub, diameter="0.4")[0] == 409  # already in the printer
    assert hub.req("GET", "/api/nozzle-requests", user=USER)[1] == []


def test_one_open_request_per_job(hub):
    install(hub)
    first = ask(hub, note="one")[1]
    again = ask(hub, user=ADMIN, note="two", type="stainless_steel")[1]
    assert again["id"] == first["id"] and again["user_id"] == USER and again["note"] == "one"
    second = ask(hub, user=ADMIN, job_id=hub.queue_job()["id"])[1]  # another job may ask for the same nozzle
    assert second["id"] != first["id"]
    assert len(hub.req("GET", "/api/nozzle-requests", user=ADMIN)[1]) == 2


def test_a_user_without_a_grant_sees_no_requests(hub):
    install(hub)
    ask(hub)
    hub.app.grants.items.clear()
    assert hub.req("GET", "/api/nozzle-requests", user=OTHER)[1] == []


def test_the_request_closes_itself_when_the_nozzle_is_installed(hub):
    install(hub)
    req = ask(hub)[1]
    assert [r["id"] for r in hub.req("GET", "/api/nozzle-requests", user=ADMIN)[1]] == [req["id"]]
    install(hub, size="0.6", type_="stainless_steel")  # right size, wrong type: still open
    assert len(hub.req("GET", "/api/nozzle-requests", user=ADMIN)[1]) == 1
    hub.ha.s.pop("sensor.lab_h2s_nozzle_size")  # unknown: still open
    assert len(hub.req("GET", "/api/nozzle-requests", user=ADMIN)[1]) == 1
    install(hub, size="0.6")
    assert hub.req("GET", "/api/nozzle-requests", user=ADMIN)[1] == []
    hub.app.nozzle_requests.__init__(hub.app.data_dir)  # the closing was saved
    assert hub.app.nozzle_requests.items == []


def test_delete_permissions(hub):
    install(hub)
    mine = ask(hub)[1]
    assert hub.req("DELETE", "/api/nozzle-requests/" + mine["id"], user=OTHER)[0] == 403  # sees the printer, not the requester
    assert hub.req("DELETE", "/api/nozzle-requests/000000000000", user=USER)[0] == 404
    assert hub.req("DELETE", "/api/nozzle-requests/" + mine["id"], user=USER)[0] == 200  # the requester
    again = ask(hub)[1]
    assert hub.req("DELETE", "/api/nozzle-requests/" + again["id"], user=ADMIN)[0] == 200  # HA admin
    third = ask(hub)[1]
    hub.owner(OTHER)
    assert hub.req("DELETE", "/api/nozzle-requests/" + third["id"], user=OTHER)[0] == 200  # printer owner
    assert hub.req("GET", "/api/nozzle-requests", user=ADMIN)[1] == []


def notes(hub):
    return [(c[1], c[2]["message"]) for c in hub.ha.calls if c[0] == "notify"]


def test_only_the_owner_at_home_is_notified_and_the_event_is_fired(hub):
    install(hub)
    hub.ha.s.update({"person.ada": st("home", user_id=ADMIN), "person.cy": st("home", user_id=OTHER)})
    hub.owner(OTHER)
    hub.req("PUT", "/api/integration/options", {"recipients": ACME}, secret=True)
    ask(hub, note="please")
    ask(hub)  # a duplicate raises nothing
    ask(hub, user=ADMIN, job_id=hub.queue_job()["id"])  # the same change for another job: the owner is not told twice
    hub.app.notifier.flush()
    assert notes(hub) == [("mobile_app_cy", "U-USER asks to put a 0.6 mm hardened steel nozzle into Lab: please")]
    (etype, data), = [e for e in hub.ha.events if e[0] == "druckwerk_nozzle_requested"]
    assert etype == "druckwerk_nozzle_requested" and data["request"]["printer"] == SERIAL and data["request"]["printer_name"] == "Lab"


def test_a_printer_without_owner_still_stores_the_request(hub):
    install(hub)
    hub.ha.s["person.ada"] = st("home", user_id=ADMIN)
    hub.req("PUT", "/api/integration/options", {"recipients": {"person.ada": "mobile_app_ada"}}, secret=True)
    req = ask(hub, type="")[1]
    hub.app.notifier.flush()
    assert notes(hub) == [] and hub.req("GET", "/api/nozzle-requests", user=ADMIN)[1] == [req]
