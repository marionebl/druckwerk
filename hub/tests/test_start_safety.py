"""A print start is safe: fresh status, one send per printer, a timeout is not a refusal, no stale confirmations."""
import threading


from app import HttpError
from hubfakes import ADMIN, OTHER, SERIAL, USER, FakeBridge, make_3mf


def idle_with_job(hub, **kw):
    hub.setup_printers()
    hub.set_print(gcode_state="IDLE")
    return hub.queue_job(**kw)


def start(hub, user=ADMIN, body=None):
    return hub.req("POST", "/api/printers/%s/start-next" % SERIAL, body or {"plate_clear": True}, user=user)


def hold_posts(hub):
    """The bridge call of a print waits for `release`; other bridge calls pass."""
    inside, release = threading.Event(), threading.Event()
    real = hub.app.bridge_json

    def slow(method, path, *a, **kw):
        if method == "POST" and path == "/api/print":
            inside.set()
            release.wait(15)
        return real(method, path, *a, **kw)
    hub.app.bridge_json = slow
    return inside, release


# -- QUEUE-06, BRIDGE-06
def test_a_start_refuses_a_status_the_bridge_says_is_300_s_old(hub):
    idle_with_job(hub)
    FakeBridge.status_age = {SERIAL: 300.0}
    status, body = start(hub)
    assert status == 409 and "older than 60 s" in body["error"], body
    assert hub.sent() == [] and hub.app.queue.jobs[0]["state"] == "queued"


def test_the_view_keeps_the_tolerance_but_shows_the_bridge_age(hub):
    hub.setup_printers()
    FakeBridge.status_age = {SERIAL: 300.0}
    p = hub.req("GET", "/api/printers", user=ADMIN)[1][0]
    assert (p["online"], p["stale"], p["status_age"], p["status"]) == (True, True, 300, "running")


def test_a_start_with_a_fresh_status_goes_on(hub):
    idle_with_job(hub)
    FakeBridge.status_age = {SERIAL: 20.0}
    assert start(hub)[0] == 200 and len(hub.sent()) == 1


def test_ticks_still_use_a_status_up_to_10_minutes_old(hub):
    idle_with_job(hub)
    assert start(hub)[0] == 200
    hub.set_print(gcode_state="RUNNING", stg_cur=4)
    FakeBridge.status_age = {SERIAL: 300.0}
    hub.app.tick_all()
    assert hub.app.queue.jobs[0]["state"] == "printing"


# -- QUEUE-05
def test_two_sends_to_one_printer_reach_the_bridge_once(hub):
    idle_with_job(hub)
    inside, release = hold_posts(hub)
    out = []
    t = threading.Thread(target=lambda: out.append(start(hub)), daemon=True)
    t.start()
    assert inside.wait(5)
    status, body = hub.req("POST", "/api/actions/print_file", {"serial": SERIAL, "file": hub.s.SHARE + "/b.gcode.3mf", "force": True}, secret=True)
    release.set()
    t.join(10)
    assert status == 409 and "already sending" in body["error"], body
    assert out[0][0] == 200 and [s["filename"] for s in hub.sent()] == ["a.gcode.3mf"]


# -- BRIDGE-10
def test_print_file_refuses_a_printer_in_prepare(hub):
    hub.setup_printers()
    hub.seed_share()
    FakeBridge.printer_extra = {SERIAL: {"print_status": "PREPARE"}}
    status, body = hub.req("POST", "/api/actions/print_file", {"serial": SERIAL, "file": hub.s.SHARE + "/a.gcode.3mf"}, secret=True)
    assert status == 409 and "PREPARE" in body["error"] and hub.sent() == []


# -- QUEUE-03
def break_the_send(hub, exc):
    """The bridge call of a print raises `exc` (a timeout), after the bridge may have taken the file."""
    real = hub.app.bridge_json

    def broken(method, path, *a, **kw):
        if method == "POST" and path == "/api/print":
            raise exc
        return real(method, path, *a, **kw)
    hub.app.bridge_json = broken


def test_a_send_that_times_out_fails_the_job(hub):
    idle_with_job(hub)
    break_the_send(hub, HttpError(502, "bridge is not reachable: timed out"))
    status, body = start(hub)
    assert status == 409 and body["error"] == "send timed out; check the printer before you retry"
    assert hub.app.queue.jobs == []
    (job,) = hub.app.queue.history
    assert (job["state"], job["error"]) == ("failed", "send timed out; check the printer before you retry")
    assert start(hub)[0] == 409  # nothing waits in the queue that a second start could send again


def test_a_bridge_answer_of_504_fails_the_job_too(hub):
    idle_with_job(hub)
    FakeBridge.post_answer = {"/api/print": (504, {"error": "daemon did not answer in time"})}
    assert start(hub)[0] == 409
    assert [j["state"] for j in hub.app.queue.history] == ["failed"] and hub.app.queue.jobs == []


def test_a_refusal_keeps_the_job_queued(hub):
    idle_with_job(hub)
    FakeBridge.post_answer = {"/api/print": (502, {"error": "cloud says no"})}
    status, body = start(hub)
    assert status == 409 and body["error"] == "cloud says no"
    assert [j["state"] for j in hub.app.queue.jobs] == ["queued"] and hub.app.queue.history == []


def test_a_model_mismatch_keeps_the_job_queued(hub):
    hub.setup_printers()
    hub.set_print(gcode_state="IDLE")
    hub.seed_share()
    make_3mf(hub.s.SHARE + "/a.gcode.3mf", gcode="; printer_model = Bambu Lab H2S\n")
    hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL}, user=ADMIN)
    assert start(hub)[0] == 409
    assert [j["state"] for j in hub.app.queue.jobs] == ["queued"]


# -- QUEUE-07
def test_start_next_from_an_action_starts_a_drying_job(hub):
    hub.setup_printers()
    hub.set_print(gcode_state="IDLE")
    hub.req("POST", "/api/queue", {"kind": "dry", "target": SERIAL, "ams_id": 0, "temp": 55, "hours": 4}, user=ADMIN)
    status, body = hub.req("POST", "/api/actions/start_next",
                           {"printer": SERIAL, "plate_clear": True, "confirmed_by_person": True}, secret=True)
    assert status == 200 and body["result"]["job"]["state"] == "drying", body


# -- QUEUE-08
def printing_without_end(hub):
    idle_with_job(hub)
    assert start(hub)[0] == 200
    hub.set_print(gcode_state="RUNNING", stg_cur=4)
    hub.app.tick_all()
    job = hub.app.queue.jobs[0]
    assert job["state"] == "printing"
    return job


def test_a_printing_job_without_an_end_state_can_be_removed_by_an_admin(hub):
    job = printing_without_end(hub)
    path = "/api/queue/%s/cancel" % job["id"]
    status, body = hub.req("POST", path, {}, user=ADMIN)
    assert status == 409 and "stop it there" in body["error"]  # a plain cancel does not pretend
    hub.grant(USER, SERIAL)
    assert hub.req("POST", path, {"force": True}, user=USER)[0] == 403
    status, body = hub.req("POST", path, {"force": True}, user=ADMIN)
    assert status == 200 and (body["state"], body["error"]) == ("failed", "removed by admin")
    assert [j["state"] for j in hub.app.queue.history] == ["failed"] and hub.app.queue.jobs == []
    hub.set_print(gcode_state="IDLE")
    hub.req("POST", "/api/queue", {"file": "b.gcode.3mf", "target": SERIAL}, user=ADMIN)
    assert start(hub)[0] == 200


def test_force_cancel_leaves_waiting_jobs_to_the_plain_rule(hub):
    idle_with_job(hub)
    job = hub.app.queue.jobs[0]
    status, body = hub.req("POST", "/api/queue/%s/cancel" % job["id"], {"force": True}, user=ADMIN)
    assert status == 200 and body["state"] == "cancelled"


# -- QUEUE-11
def test_the_owner_of_another_printer_cannot_approve_an_any_job(hub):
    hub.setup_printers()
    hub.app.registry.put("FAKE0003", name="Third", model="H2S", account="a_x", prefix="lab_h2s_3")
    hub.owner(OTHER, SERIAL)  # Cy owns one H2S; Ben may use the other one, with approval
    hub.grant(USER, "FAKE0003", needs_approval=True)
    hub.seed_share()
    job = hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": "any", "model": "H2S"}, user=USER)[1]
    assert job["state"] == "pending_approval"
    status, body = hub.req("POST", "/api/queue/%s/approve" % job["id"], {}, user=OTHER)
    assert status == 403 and "approve" in body["error"]
    hub.grant(USER, SERIAL, needs_approval=True)  # now the job fits Cy's printer too
    assert hub.req("POST", "/api/queue/%s/approve" % job["id"], {}, user=OTHER)[1]["state"] == "queued"


def test_an_ha_admin_approves_an_any_job_of_a_lapsed_grant(hub):
    hub.setup_printers()
    hub.app.registry.put("FAKE0003", name="Third", model="H2S", account="a_x", prefix="lab_h2s_3")
    g = hub.grant(USER, "FAKE0003", needs_approval=True)
    hub.seed_share()
    job = hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": "any", "model": "H2S"}, user=USER)[1]
    hub.app.grants.remove(g["id"])
    assert hub.req("POST", "/api/queue/%s/approve" % job["id"], {}, user=ADMIN)[1]["state"] == "queued"


# -- CORE-16
def test_two_jobs_with_the_same_name_get_two_share_files(hub):
    hub.setup_printers()
    one, two = hub.sliced(owner=ADMIN, jid="a1b2c3d4e5f6"), hub.sliced(owner=ADMIN, jid="b1b2c3d4e5f6")
    names = []
    real = hub.app.sender.print_file

    def spy(data):
        names.append(data["file"])
        return real(data)
    hub.s.PRINT = spy
    for jid in (one, two):
        assert hub.req("POST", "/api/jobs/%s/print" % jid, {"confirm": True}, user=ADMIN)[0] == 200
    assert len(set(names)) == 2, names


def test_a_sent_copy_stays_for_the_printer_picture_until_the_slice_is_sent_again(hub):
    import os
    hub.setup_printers()
    jid = hub.sliced(owner=ADMIN)
    assert hub.req("POST", "/api/jobs/%s/print" % jid, {"confirm": True}, user=ADMIN)[0] == 200
    first = next(iter(hub.app.sender.results.values()))["last_path"]
    assert os.path.exists(first)  # integration.wanted and the preview read it while the print runs
    assert hub.req("POST", "/api/jobs/%s/print" % jid, {"confirm": True}, user=ADMIN)[0] == 200
    assert [f for f in os.listdir(hub.s.SHARE) if f.endswith(".gcode.3mf")] == [os.path.basename(
        next(iter(hub.app.sender.results.values()))["last_path"])]


def test_a_refused_send_leaves_no_copy(hub):
    import os

    def refuse(data):
        raise ValueError("model mismatch")
    hub.setup_printers()
    jid = hub.sliced(owner=ADMIN)
    hub.s.PRINT = refuse
    assert hub.req("POST", "/api/jobs/%s/print" % jid, {"confirm": True}, user=ADMIN)[0] == 400
    assert not [f for f in os.listdir(hub.s.SHARE) if f.endswith(".gcode.3mf")]
