"""No call to Home Assistant, the bridge or the cloud runs while a lock of the queue is held."""
import threading
import time

from hubfakes import ADMIN, SERIAL, USER, wait_threads

LIMIT = 0.5  # seconds a read may take while a slow call runs


def in_thread(fn, *a, **kw):
    out = []
    t = threading.Thread(target=lambda: out.append(fn(*a, **kw)), daemon=True)
    t.start()
    return t, out


def timed(hub, path, secret=False):
    t0 = time.time()
    status = hub.req("GET", path, user=None if secret else ADMIN, secret=secret)[0]
    return status, time.time() - t0


def blocker(owner, name):
    """Replace owner.<name> by a function that waits for `release` before it calls the real one."""
    inside, release = threading.Event(), threading.Event()
    real = getattr(owner, name)

    def slow(*a, **kw):
        inside.set()
        release.wait(15)
        return real(*a, **kw)
    setattr(owner, name, slow)
    return inside, release


def queue_two(hub):
    hub.setup_printers()
    hub.seed_share()
    hub.req("POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL}, user=ADMIN)
    hub.req("POST", "/api/queue", {"file": "b.gcode.3mf", "target": SERIAL}, user=ADMIN)
    hub.set_print(gcode_state="IDLE")


def test_queue_and_events_answer_while_start_next_sends(hub):
    queue_two(hub)
    inside, release = blocker(hub.app.queue, "send_print")
    t, out = in_thread(hub.req, "POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user=ADMIN)
    assert inside.wait(5)
    results = [timed(hub, "/api/queue"), timed(hub, "/api/events", secret=True)]
    release.set()
    t.join(10)
    assert [s for s, _ in results] == [200, 200] and all(d < LIMIT for _, d in results), results
    assert out[0][0] == 200 and out[0][1]["state"] == "starting"


def test_four_parallel_start_next_send_one_print(hub):
    queue_two(hub)
    runs = [in_thread(hub.req, "POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user=ADMIN) for _ in range(4)]
    for t, _ in runs:
        t.join(15)
    assert sorted(o[0][0] for _, o in runs) == [200, 409, 409, 409]
    assert [s["filename"] for s in hub.sent()] == ["a.gcode.3mf"]


def test_queue_and_events_answer_while_a_finished_job_settles(hub):
    hub.cloud_spool(type="PETG", colour="ADB1B2", grams_left=500, printer=SERIAL, unit=0, slot=0)
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    jid = hub.sliced(owner=USER)
    hub.req("POST", "/api/jobs/%s/queue" % jid, {"target": SERIAL}, user=USER)
    hub.set_print(gcode_state="IDLE")
    hub.req("POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user=ADMIN)
    hub.set_print(stg_cur=4)
    hub.app.tick_all()
    inside, release = blocker(hub.app, "printer_spools")
    hub.set_print(gcode_state="FINISH")
    t, _ = in_thread(hub.app.tick_all)
    assert inside.wait(5)
    results = [timed(hub, "/api/queue"), timed(hub, "/api/events", secret=True)]
    release.set()
    t.join(10)
    assert [s for s, _ in results] == [200, 200] and all(d < LIMIT for _, d in results), results
    done = hub.req("GET", "/api/queue", user=ADMIN)[1]["history"][0]
    assert done["state"] == "done" and done["spools_used"] and done["cost"]
    types = [e["type"] for e in hub.req("GET", "/api/events?since=0", secret=True)[1]["events"]]
    assert types.count("job_done") == 1


def test_a_blocked_send_from_the_notification_does_not_stop_the_next_event(hub):
    queue_two(hub)
    inside, release = blocker(hub.app.queue, "send_print")
    t0 = time.time()
    jid = hub.app.queue.jobs[0]["id"]
    hub.app.notifier.pushed[(SERIAL, jid)] = hub.now  # as the notification of a finished job records it
    hub.app.notifier.on_action({"action": "DRUCKWERK_START_%s_%s" % (SERIAL, jid)}, ADMIN)
    assert time.time() - t0 < LIMIT  # on_action returned while the send still runs
    assert inside.wait(5)
    release.set()
    wait_threads("druckwerk-start")
    assert [j["state"] for j in hub.app.queue.jobs] == ["starting", "queued"]
