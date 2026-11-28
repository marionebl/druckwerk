"""Asking the printer owner to put a library spool into the AMS: routes, notification, closing."""
import json
import os

import pytest

from hubfakes import ADMIN, OTHER, SERIAL, SERIAL2, USER, st


@pytest.fixture
def hub(hub):
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    hub.grant(OTHER, SERIAL)
    hub.spool = hub.cloud_spool(brand="Acme", type="PLA", name="PLA Basic", colour="FF0000")
    hub.job = hub.queue_job(user=USER)  # a request belongs to a queue entry
    return hub


def ask(hub, user=USER, printer=SERIAL, note=None, spool=None, job=...):
    body = {"printer": printer, "job_id": (hub.job["id"] if job is ... else job), **({"note": note} if note is not None else {})}
    return hub.req("POST", "/api/spools/%s/load-request" % (spool or hub.spool["id"]), body, user=user)


def test_a_user_asks_and_the_request_is_listed(hub):
    status, req = ask(hub, note="for the red benchy")
    assert status == 200
    assert req["spool_id"] == hub.spool["id"] and req["printer"] == SERIAL and req["user_id"] == USER
    assert req["job_id"] == hub.job["id"] and req["job_name"] == hub.job["name"]
    assert req["spool_label"] == "Acme PLA PLA Basic" and req["note"] == "for the red benchy" and req["user_name"] == "U-USER"
    assert hub.req("GET", "/api/load-requests", user=USER)[1] == [req]
    assert hub.req("GET", "/api/load-requests", user=ADMIN)[1] == [req]
    with open(os.path.join(hub.tmp, "data", "load_requests.json")) as f:
        assert json.load(f) == [req]


def test_validation(hub):
    assert ask(hub, printer=SERIAL2)[0] == 403  # cannot see that printer
    hub.grant(USER, SERIAL2)
    assert ask(hub, printer=SERIAL2)[0] == 400  # no AMS there
    assert ask(hub, printer="NOPE")[0] == 404
    assert ask(hub, spool="999")[0] == 404
    assert ask(hub, note="x" * 201)[0] == 400
    assert hub.req("POST", "/api/spools/%s/load-request" % hub.spool["id"], {"job_id": hub.job["id"]}, user=USER)[0] == 400
    assert ask(hub, job=None)[0] == 400 and ask(hub, job="../x")[0] == 400  # a request needs a job
    assert ask(hub, job="0" * 12)[0] == 404  # no such job
    assert ask(hub, user=OTHER)[0] == 403  # not their job
    other = hub.queue_job(target=SERIAL2)
    hub.grant(USER, SERIAL2)
    assert ask(hub, job=other["id"])[0] == 400  # that job prints on another printer
    assert ask(hub, printer=SERIAL2, job=other["id"])[0] == 403  # an admin's job: the user does not own it
    assert hub.req("GET", "/api/load-requests", user=USER)[1] == []
    loaded = hub.cloud_spool(type="PETG", printer=SERIAL, unit=0, slot=0)
    hub.now += 31
    assert ask(hub, spool=loaded["id"])[0] == 409  # already in that printer


def test_one_open_request_per_job(hub):
    first = ask(hub, note="one")[1]
    again = ask(hub, user=ADMIN, note="two")[1]
    assert again["id"] == first["id"] and again["user_id"] == USER and again["note"] == "one"
    assert len(hub.req("GET", "/api/load-requests", user=ADMIN)[1]) == 1
    second = ask(hub, user=ADMIN, job=hub.queue_job()["id"])[1]  # another job may ask for the same spool
    assert second["id"] != first["id"] and len(hub.req("GET", "/api/load-requests", user=ADMIN)[1]) == 2


def test_a_user_without_a_grant_sees_no_requests(hub):
    ask(hub)
    hub.app.grants.items.clear()
    assert hub.req("GET", "/api/load-requests", user=OTHER)[1] == []


def test_the_library_showing_the_spool_in_the_printer_closes_the_request(hub):
    req = ask(hub)[1]
    hub.now += 31
    hub.req("GET", "/api/spools", user=ADMIN)
    assert [r["id"] for r in hub.req("GET", "/api/load-requests", user=ADMIN)[1]] == [req["id"]]  # not loaded yet
    hub.spool.update(printer=SERIAL2, unit=0, slot=1, in_printer=True)  # in another printer: still open
    hub.now += 31
    hub.req("GET", "/api/spools", user=ADMIN)
    assert len(hub.req("GET", "/api/load-requests", user=ADMIN)[1]) == 1
    hub.spool.update(printer=SERIAL, unit=0, slot=1, in_printer=True)
    hub.now += 31
    hub.req("GET", "/api/spools", user=ADMIN)
    assert hub.req("GET", "/api/load-requests", user=ADMIN)[1] == []
    hub.app.loads.__init__(hub.app.data_dir)  # the closing was saved
    assert hub.app.loads.items == []


def test_delete_permissions(hub):
    mine = ask(hub)[1]
    assert hub.req("DELETE", "/api/load-requests/" + mine["id"], user=OTHER)[0] == 403  # sees the printer, not the requester
    assert hub.req("DELETE", "/api/load-requests/nope", user=USER)[0] == 404
    assert hub.req("DELETE", "/api/load-requests/" + mine["id"], user=USER)[0] == 200  # the requester
    assert hub.req("GET", "/api/load-requests", user=ADMIN)[1] == []
    again = ask(hub)[1]
    assert hub.req("DELETE", "/api/load-requests/" + again["id"], user=ADMIN)[0] == 200  # HA admin
    third = ask(hub)[1]
    hub.owner(OTHER)
    assert hub.req("DELETE", "/api/load-requests/" + third["id"], user=OTHER)[0] == 200  # printer owner


RECIPIENTS = {"person.ada": "notify.mobile_app_ada", "person.ben": "mobile_app_ben", "person.cy": "mobile_app_cy"}


def notes(hub):
    return [(c[1], c[2]) for c in hub.ha.calls if c[0] == "notify"]


def test_only_owners_at_home_are_notified_and_the_event_is_fired(hub):
    hub.ha.s.update({"person.ada": st("home", user_id=ADMIN), "person.ben": st("home", user_id=USER), "person.cy": st("home", user_id=OTHER)})
    hub.owner(OTHER)
    hub.req("PUT", "/api/integration/options", {"recipients": RECIPIENTS}, secret=True)
    ask(hub, note="please")
    ask(hub)  # a duplicate raises nothing
    ask(hub, user=ADMIN, job=hub.queue_job()["id"])  # the same change for another job: the owner is not told twice
    hub.app.notifier.flush()
    # Ada is an HA admin but no owner of the printer: she gets nothing.
    assert [(s, d["message"]) for s, d in notes(hub)] == [
        ("mobile_app_cy", "U-USER asks to load Acme PLA PLA Basic into Lab: please")]
    (etype, data), = [e for e in hub.ha.events if e[0] == "druckwerk_load_requested"]  # one, not one per job
    assert etype == "druckwerk_load_requested" and data["request"]["printer"] == SERIAL


def test_a_different_spool_for_the_same_printer_is_a_new_notification(hub):
    hub.ha.s["person.cy"] = st("home", user_id=OTHER)
    hub.owner(OTHER)
    hub.req("PUT", "/api/integration/options", {"recipients": {"person.cy": "mobile_app_cy"}}, secret=True)
    ask(hub)
    petg = hub.cloud_spool(type="PETG")
    hub.now += 31  # the library is cached
    ask(hub, user=ADMIN, job=hub.queue_job()["id"], spool=petg["id"])
    hub.app.notifier.flush()
    assert len(notes(hub)) == 2


def test_the_owner_is_notified_without_a_note_and_the_owner_of_another_printer_is_not(hub):
    hub.ha.s.update({"person.cy": st("home", user_id=OTHER), "person.ada": st("home", user_id=ADMIN)})
    hub.owner(OTHER)
    hub.owner(ADMIN, SERIAL2)  # owns the other printer: no business with this request
    hub.req("PUT", "/api/integration/options", {"recipients": {"person.cy": "mobile_app_cy", "person.ada": "mobile_app_ada"}}, secret=True)
    ask(hub)
    hub.app.notifier.flush()
    assert [(s, d["message"]) for s, d in notes(hub)] == [("mobile_app_cy", "U-USER asks to load Acme PLA PLA Basic into Lab")]  # no note, no colon


def test_a_printer_without_owner_still_stores_the_request(hub):
    hub.ha.s["person.ada"] = st("home", user_id=ADMIN)
    hub.req("PUT", "/api/integration/options", {"recipients": {"person.ada": "mobile_app_ada"}}, secret=True)
    req = ask(hub)[1]
    hub.app.notifier.flush()
    assert notes(hub) == [] and hub.req("GET", "/api/load-requests", user=ADMIN)[1] == [req]
