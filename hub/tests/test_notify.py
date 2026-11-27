"""Notifications and HA bus events from the app, the options the integration pushes, and the notification button."""
import json
import os

import pytest

from hubfakes import ADMIN, OTHER, SERIAL, USER, st

RECIPIENTS = {"person.ada": "notify.mobile_app_ada", "person.ben": "mobile_app_ben", "person.cy": "mobile_app_cy",
              "person.far": "mobile_app_far", "person.gone": "mobile_app_gone"}


@pytest.fixture
def hub(hub):
    hub.setup_printers()
    hub.seed_share()
    hub.ha.s.update({
        "person.ada": st("home", user_id=ADMIN),  # HA admin
        "person.ben": st("home", user_id=USER),  # no grant: not an admin
        "person.cy": st("home", user_id=OTHER),  # owner of the printer, set below
        "person.far": st("not_home", user_id=ADMIN),  # an admin who is away
    })
    hub.owner(OTHER)
    hub.set_print(gcode_state="IDLE")
    return hub


def push(hub, recipients=RECIPIENTS):
    return hub.req("PUT", "/api/integration/options", {"recipients": recipients}, secret=True)


def notes(hub):
    return [(c[1], c[2]) for c in hub.ha.calls if c[0] == "notify"]


def finish_first_job(hub):
    """Two jobs for the printer: start the first, let it finish. Returns the second job."""
    first = hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL, "name": "First"}, user=ADMIN)[1]
    second = hub.req("POST", "/api/queue", {"file": "b.gcode.3mf", "target": SERIAL, "name": "Second"}, user=ADMIN)[1]
    hub.req("POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user=ADMIN)
    hub.set_print(stg_cur=4)
    hub.app.tick_all()
    hub.set_print(gcode_state="FINISH")
    hub.app.tick_all()
    return first, second


def test_options_are_internal_stored_and_validated(hub):
    assert hub.req("PUT", "/api/integration/options", {"recipients": {}}, user=ADMIN)[0] == 403
    status, body = push(hub)
    assert status == 200 and body == {"recipients": RECIPIENTS}
    for bad in ({"recipients": []}, {"recipients": {"person.a": 1}}, {}, {"recipients": {"": "x"}}):
        assert hub.req("PUT", "/api/integration/options", bad, secret=True)[0] == 400
    assert hub.app.notifier.options["recipients"] == RECIPIENTS  # a refused push changes nothing
    with open(os.path.join(hub.tmp, "data", "integration.json")) as f:
        assert json.load(f) == {"recipients": RECIPIENTS}


def test_options_survive_a_restart(hub):
    push(hub)
    from notify import Notifier
    assert Notifier(hub.app).options["recipients"] == RECIPIENTS


def test_job_done_with_a_next_job_asks_admins_who_are_home_to_start_it(hub):
    push(hub)
    finish_first_job(hub)
    hub.app.notifier.flush()
    sent = notes(hub)
    # Ada (HA admin) and Cy (owner of the printer) are home; Ben is no admin, Far is away, Gone has no state.
    assert [s for s, _ in sent] == ["mobile_app_ada", "mobile_app_cy"]
    data = sent[0][1]
    assert data["title"] == "Druckwerk" and data["message"] == "Plate clear? Start next: Second"
    assert data["data"] == {"actions": [{"action": "DRUCKWERK_START_" + SERIAL, "title": "Plate clear, start"}]}


def test_no_notification_without_a_next_job_or_recipients(hub):
    push(hub)
    hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL}, user=ADMIN)
    hub.req("POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user=ADMIN)
    hub.set_print(stg_cur=4)
    hub.app.tick_all()
    hub.set_print(gcode_state="FINISH")
    hub.app.tick_all()
    hub.app.notifier.flush()
    assert notes(hub) == []
    push(hub, {})
    finish_first_job(hub)
    hub.app.notifier.flush()
    assert notes(hub) == []


def test_a_waiting_drying_is_not_offered_as_the_next_plate(hub):
    push(hub)
    hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL}, user=ADMIN)
    hub.req("POST", "/api/queue", {"kind": "dry", "target": SERIAL, "ams_id": 0, "temp": 55, "hours": 4}, user=ADMIN)
    hub.req("POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user=ADMIN)
    hub.set_print(stg_cur=4)
    hub.app.tick_all()
    hub.set_print(gcode_state="FINISH")
    hub.app.tick_all()
    hub.app.notifier.flush()
    assert notes(hub) == []


def test_approval_needed_notifies_admins(hub):
    push(hub)
    hub.grant(USER, SERIAL, needs_approval=True)
    hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL, "name": "Benchy"}, user=USER)
    hub.app.notifier.flush()
    assert [(s, d["message"], d["data"]) for s, d in notes(hub)] == [
        ("mobile_app_ada", "U-USER needs approval to print Benchy", {}), ("mobile_app_cy", "U-USER needs approval to print Benchy", {})]


def test_the_notification_button_starts_the_next_job_with_plate_clear(hub):
    _, second = finish_first_job(hub)
    hub.app.notifier.on_action({"action": "DRUCKWERK_START_" + SERIAL})
    states = {j["id"]: j["state"] for j in hub.app.queue.jobs + hub.app.queue.history}
    assert states[second["id"]] == "starting"
    assert [s["filename"] for s in hub.sent()] == ["a.gcode.3mf", "b.gcode.3mf"]


def test_other_actions_and_a_refused_start_are_ignored(hub):
    hub.app.notifier.on_action({"action": "SOMETHING_ELSE"})
    hub.app.notifier.on_action({"action": "DRUCKWERK_START_" + SERIAL})  # nothing queued: logged, not raised
    assert hub.sent() == []


def test_every_event_is_fired_on_the_ha_bus_with_its_payload(hub):
    finish_first_job(hub)
    hub.app.notifier.flush()
    fired = [(t, d["type"], d["serial"]) for t, d in hub.ha.events]
    assert ("druckwerk_job_started", "job_started", SERIAL) in fired
    assert ("druckwerk_job_done", "job_done", SERIAL) in fired
    assert ("druckwerk_queue_changed", "queue_changed", "") in fired
    done = next(d for t, d in hub.ha.events if t == "druckwerk_job_done")
    assert done["job"]["name"] == "First" and done["seq"] > 0 and set(done) == {"seq", "type", "serial", "job"}


def test_a_failing_bus_does_not_stop_the_notification(hub):
    push(hub)
    hub.ha.fail["fire_event"] = "HA down"
    finish_first_job(hub)
    hub.app.notifier.flush()
    assert len(notes(hub)) == 2 and hub.ha.events == []


def test_one_broken_phone_does_not_stop_the_others(hub):
    push(hub)
    hub.ha.fail[("notify", "mobile_app_ada")] = "no push"
    finish_first_job(hub)
    hub.app.notifier.flush()
    assert [s for s, _ in notes(hub)] == ["mobile_app_ada", "mobile_app_cy"]  # both tried, one failed
