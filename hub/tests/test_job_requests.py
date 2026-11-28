"""A nozzle or spool change is requested for a queue entry; the entry is blocked until the change happened."""
import pytest

from hubfakes import ADMIN, OTHER, SERIAL, USER, st

SIZE = "sensor.lab_h2s_nozzle_size"


@pytest.fixture
def hub(hub):
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    hub.spool = hub.cloud_spool(brand="Acme", type="PLA", name="PLA Basic", colour="FF0000")
    hub.ha.s[SIZE] = st("0.4", unit_of_measurement="mm")
    hub.job = hub.queue_job(user=USER)
    return hub


def ask_nozzle(hub, job=None, **kw):
    return hub.req("POST", "/api/printers/%s/nozzle-request" % SERIAL,
                   {"diameter": "0.6", "type": "", "job_id": (job or hub.job)["id"], **kw}, user=USER)


def ask_load(hub, job=None, **kw):
    return hub.req("POST", "/api/spools/%s/load-request" % hub.spool["id"],
                   {"printer": SERIAL, "job_id": (job or hub.job)["id"], **kw}, user=USER)


def start_next(hub):
    hub.set_print(gcode_state="IDLE")
    return hub.req("POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user=ADMIN)


def start_job(hub, job=None):
    hub.set_print(gcode_state="IDLE")
    return hub.req("POST", "/api/printers/%s/start" % SERIAL, {"plate_clear": True, "job_id": (job or hub.job)["id"]}, user=ADMIN)


def queued(hub, job=None):
    return next(j for j in hub.req("GET", "/api/queue", user=ADMIN)[1]["jobs"] if j["id"] == (job or hub.job)["id"])


def test_a_request_needs_a_job(hub):
    assert hub.req("POST", "/api/printers/%s/nozzle-request" % SERIAL, {"diameter": "0.6", "type": ""}, user=USER)[0] == 400
    assert hub.req("POST", "/api/spools/%s/load-request" % hub.spool["id"], {"printer": SERIAL}, user=USER)[0] == 400


@pytest.mark.parametrize("kind", ["nozzle", "load"])
def test_a_job_with_an_open_request_refuses_every_start_and_shows_why(hub, kind):
    req = (ask_nozzle if kind == "nozzle" else ask_load)(hub, note="please")[1]
    wait = "0.6 mm nozzle" if kind == "nozzle" else "Acme PLA PLA Basic to be loaded"
    for status, body in (start_next(hub), start_job(hub)):
        assert status == 409 and body["error"] == "This job is waiting for %s." % wait
    assert hub.sent() == [] and queued(hub)["state"] == "queued"
    (shown,) = queued(hub)["requests"]
    assert (shown["id"], shown["kind"], shown["wait"], shown["user_name"], shown["note"]) == (req["id"], kind, wait, "U-USER", "please")


def test_the_next_job_starts_past_a_blocked_one(hub):
    ask_nozzle(hub)
    second = hub.queue_job(file="b")
    status, body = start_next(hub)
    assert status == 200 and body["id"] == second["id"] and queued(hub)["requests"][0]["kind"] == "nozzle"


def test_the_change_unblocks_the_job(hub):
    ask_nozzle(hub)
    hub.ha.s[SIZE] = st("0.6")
    assert queued(hub)["requests"] == [] and start_job(hub)[0] == 200  # the queue read closes it, then the start goes through


def test_the_spool_in_the_printer_unblocks_the_job(hub):
    ask_load(hub)
    assert start_job(hub)[0] == 409
    hub.spool.update(printer=SERIAL, unit=0, slot=1, in_printer=True)
    hub.now += 31  # the library is cached
    assert start_job(hub)[0] == 200


def test_cancelling_the_request_unblocks_but_the_nozzle_check_stays(hub):
    hub.app.queue.jobs[0]["nozzle"] = "0.6"
    req = ask_nozzle(hub)[1]
    assert hub.req("DELETE", "/api/nozzle-requests/" + req["id"], user=USER)[0] == 200
    assert queued(hub)["requests"] == []
    status, body = start_job(hub)
    assert status == 409 and body["error"] == "This job needs a 0.6 mm nozzle; the printer has 0.4 mm."
    load = ask_load(hub)[1]
    assert hub.req("DELETE", "/api/load-requests/" + load["id"], user=ADMIN)[0] == 200
    assert queued(hub)["requests"] == []


def test_a_cancelled_job_closes_its_requests(hub):
    ask_nozzle(hub)
    ask_load(hub)
    assert hub.req("POST", "/api/queue/%s/cancel" % hub.job["id"], user=ADMIN)[0] == 200
    assert hub.req("GET", "/api/nozzle-requests", user=ADMIN)[1] == []
    assert hub.req("GET", "/api/load-requests", user=ADMIN)[1] == []


def test_a_request_without_a_job_is_listed_and_cancellable_but_blocks_nothing(hub):
    legacy = {"id": "c" * 12, "spool_id": hub.spool["id"], "spool_label": "Acme PLA", "colour": "FF0000", "printer": SERIAL,
              "user_id": USER, "user_name": "U-USER", "note": "", "created_at": 1}  # saved before requests had jobs
    hub.app.loads.items.append(legacy)
    assert [r["id"] for r in hub.req("GET", "/api/load-requests", user=ADMIN)[1]] == [legacy["id"]]
    assert queued(hub)["requests"] == [] and start_job(hub)[0] == 200
    assert hub.req("DELETE", "/api/load-requests/" + legacy["id"], user=USER)[0] == 200
    assert hub.req("GET", "/api/load-requests", user=ADMIN)[1] == []


def test_only_the_owner_or_an_admin_asks_for_a_job(hub):
    hub.grant(OTHER, SERIAL)
    assert hub.req("POST", "/api/printers/%s/nozzle-request" % SERIAL, {"diameter": "0.6", "type": "", "job_id": hub.job["id"]}, user=OTHER)[0] == 403
    assert hub.req("POST", "/api/printers/%s/nozzle-request" % SERIAL, {"diameter": "0.6", "type": "", "job_id": hub.job["id"]}, user=ADMIN)[0] == 200
