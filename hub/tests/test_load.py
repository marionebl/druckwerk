"""Constant load: how often the hub's own polling reaches the bridge and Home Assistant."""
import http.client
import threading
import time
from collections import Counter

from hubfakes import ADMIN, SERIAL, SERIAL2

from ha import HAError


def count_bridge(hub):
    """Counter of (method, path without query) for every bridge call of the app."""
    calls, real = Counter(), hub.app.bridge_json

    def counted(method, path, *a, **kw):
        calls[(method, path.split("?")[0])] += 1
        return real(method, path, *a, **kw)
    hub.app.bridge_json = counted
    return calls


def printing(hub):
    hub.setup_printers()
    hub.queue_job()
    hub.set_print(gcode_state="IDLE")
    assert hub.req("POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user=ADMIN)[0] == 200
    hub.set_print(gcode_state="RUNNING", stg_cur=0)
    hub.now += 30


def test_a_ticker_pass_reads_each_printer_status_once(hub):
    printing(hub)
    calls = count_bridge(hub)
    hub.app.tick_all()
    print("bridge calls per ticker pass:", sum(calls.values()), dict(calls))
    assert calls[("GET", "/api/status/" + SERIAL)] == 1
    assert calls[("GET", "/api/status/" + SERIAL2)] <= 1


def test_the_integration_and_the_ui_share_one_printer_list_read(hub):
    hub.setup_printers()
    hub.app.printer_status(SERIAL)
    calls = count_bridge(hub)
    assert hub.req("GET", "/api/integration/state", secret=True)[0] == 200
    assert hub.req("GET", "/api/printers", user=ADMIN)[0] == 200
    print("bridge calls per integration poll plus UI poll:", sum(calls.values()), dict(calls))
    assert calls[("GET", "/api/printers")] == 1
    hub.now += 6
    assert hub.req("GET", "/api/integration/state", secret=True)[0] == 200
    assert calls[("GET", "/api/printers")] == 2


def test_a_reused_status_is_never_older_than_the_callers_max_age(hub):
    hub.setup_printers()
    calls = count_bridge(hub)
    path = ("GET", "/api/status/" + SERIAL)
    hub.app.printer_status(SERIAL)
    hub.now += 1.5
    hub.app.printer_status(SERIAL)
    assert calls[path] == 1  # inside 2 s: reused
    hub.app.printer_status(SERIAL, max_age=1)
    assert calls[path] == 2  # a view of 1.5 s is too old for max_age 1
    hub.now += 2.5
    hub.app.printer_status(SERIAL)
    assert calls[path] == 3


def test_eight_parallel_requests_make_one_call_to_home_assistant(hub):
    hub.app._cache.clear()
    real, calls = hub.ha.command, Counter()

    def slow(type_, **kw):
        calls[type_] += 1
        time.sleep(0.3)
        return real(type_, **kw)
    hub.ha.command = slow
    ts = [threading.Thread(target=hub.app.get_user, args=(ADMIN,)) for _ in range(8)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert dict(calls) == {"config/auth/list": 1}


def test_a_slow_key_does_not_block_another_key(hub):
    release = threading.Event()
    t = threading.Thread(target=hub.app._cached, args=("slow", 5, lambda: release.wait(10) or 1), daemon=True)
    t.start()
    time.sleep(0.2)
    t0 = time.time()
    assert hub.app._cached("fast", 5, lambda: 2) == 2
    release.set()
    t.join(5)
    assert time.time() - t0 < 0.5


def test_a_failure_is_remembered_for_5_s(hub):
    calls = []

    def boom():
        calls.append(1)
        raise HAError("down")
    for _ in range(3):
        try:
            hub.app._cached("k", 30, boom)
            assert False, "expected HAError"
        except HAError as e:
            assert "down" in str(e)
    assert len(calls) == 1
    hub.now += 6
    try:
        hub.app._cached("k", 30, boom)
    except HAError:
        pass
    assert len(calls) == 2
    hub.now += 6
    assert hub.app._cached("k", 30, lambda: 7) == 7  # a later success replaces the failure


def get(hub, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=20)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


def test_vendor_files_carry_an_etag_and_answer_304(hub):
    path = "/vendor/three.module.min.js"
    status, head, body = get(hub, path)
    assert status == 200 and body and head["Cache-Control"] == "no-cache" and head["ETag"]
    status, head2, body = get(hub, path, {"If-None-Match": head["ETag"]})
    assert (status, body) == (304, b"") and head2["ETag"] == head["ETag"]
    assert get(hub, path, {"If-None-Match": '"other"'})[0] == 200


def test_the_app_code_stays_no_store(hub):
    for path in ("/", "/app.js"):
        status, head, _ = get(hub, path, {"If-None-Match": "*"})
        assert status == 200 and head["Cache-Control"] == "no-store"
