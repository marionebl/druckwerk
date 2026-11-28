"""The stdlib websocket client against a minimal fake HA websocket server."""
import base64
import hashlib
import json
import socket
import struct
import threading

import pytest

from ha import GUID, HA, HAError, HAUnavailable


def read_exact(c, n):
    out = b""
    while len(out) < n:
        chunk = c.recv(n - len(out))
        if not chunk:
            raise OSError("closed")
        out += chunk
    return out


def read_frame(c):
    b0, b1 = read_exact(c, 2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", read_exact(c, 2))[0]
    mask = read_exact(c, 4)
    assert b1 & 0x80, "client frames must be masked"
    return json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(read_exact(c, n))))


def write_frame(c, obj, big=False):
    data = json.dumps(obj).encode()
    head = bytes([0x81]) + (bytes([127]) + struct.pack(">Q", len(data)) if big else
                            bytes([126]) + struct.pack(">H", len(data)) if len(data) > 125 else bytes([len(data)]))
    c.sendall(head + data)


@pytest.fixture
def server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    seen = []

    def run():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            req = b""
            while b"\r\n\r\n" not in req:
                req += c.recv(1024)
            key = [l for l in req.decode().split("\r\n") if l.lower().startswith("sec-websocket-key")][0].split(": ")[1]
            acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
            write_frame(c, {"type": "auth_required"})
            auth = read_frame(c)
            seen.append(auth)
            if auth.get("access_token") != "tok":
                write_frame(c, {"type": "auth_invalid"})
                continue
            write_frame(c, {"type": "auth_ok"})
            msg = read_frame(c)
            seen.append(msg)
            if msg["type"] == "subscribe_events":
                c.sendall(bytes([0x89, 0]))  # a ping first: must be answered or skipped
                write_frame(c, {"id": 1, "type": "event", "event": {"event_type": "state_changed", "data": {
                    "entity_id": "sensor.x_current_stage", "new_state": {"state": "changing_filament"}}}})
                c.close()
            elif msg["type"] == "boom":
                write_frame(c, {"id": 1, "type": "result", "success": False, "error": {"message": "nope"}})
            else:
                write_frame(c, {"id": 1, "type": "result", "success": True, "result": [{"n": "x" * 70000}]}, big=True)
            c.close()
    threading.Thread(target=run, daemon=True).start()
    yield "ws://127.0.0.1:%d/core/websocket" % srv.getsockname()[1], seen
    srv.close()


def test_command_roundtrip_with_a_large_frame(server):
    url, seen = server
    result = HA("http://x", url, "tok").command("config/auth/list", extra=1)
    assert result == [{"n": "x" * 70000}]
    assert seen[0] == {"type": "auth", "access_token": "tok"} and seen[1] == {"extra": 1, "id": 1, "type": "config/auth/list"}


def test_errors(server):
    url, _ = server
    with pytest.raises(HAError, match="nope"):
        HA("http://x", url, "tok").command("boom")
    with pytest.raises(HAError, match="refused"):
        HA("http://x", url, "bad").command("x")


def test_watch_state_changes(server):
    url, _ = server
    got, stop = [], threading.Event()
    ha = HA("http://x", url, "tok")

    def cb(eid, state):
        got.append((eid, state))
        stop.set()
    ha.watch_state_changes(cb, stop)
    assert got == [("sensor.x_current_stage", "changing_filament")]


def test_unreachable_ha_is_an_haerror_not_a_crash():
    with pytest.raises(HAError, match="restarting"):
        HA("http://x", "ws://127.0.0.1:1/core/websocket", "tok").command("config/auth/list")


def test_watch_events_subscribes_to_the_type_and_hands_over_the_data(server):
    url, seen = server
    got, stop = [], threading.Event()

    def cb(data):
        got.append(data)
        stop.set()
    HA("http://x", url, "tok").watch_events("mobile_app_notification_action", cb, stop)
    assert seen[1] == {"id": 1, "type": "subscribe_events", "event_type": "mobile_app_notification_action"}
    assert got == [{"entity_id": "sensor.x_current_stage", "new_state": {"state": "changing_filament"}}]


def test_fire_event_posts_to_the_rest_api(monkeypatch):
    calls = []
    ha = HA("http://core/api", "ws://x", "tok")
    monkeypatch.setattr(ha, "_rest", lambda method, path, body=None, timeout=30: calls.append((method, path, body)))
    ha.fire_event("druckwerk_job_done", {"seq": 1})
    assert calls == [("POST", "/events/druckwerk_job_done", {"seq": 1})]


# ---- the watcher survives a failing callback and a silent connection
def read_any(c):
    b0, b1 = read_exact(c, 2)
    n = b1 & 0x7F
    mask = read_exact(c, 4)
    return b0 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(read_exact(c, n)))


def event(n):
    return {"id": 1, "type": "event", "event": {"event_type": "t", "data": {"n": n}}}


def handshake(c):
    req = b""
    while b"\r\n\r\n" not in req:
        req += c.recv(1024)
    key = [l for l in req.decode().split("\r\n") if l.lower().startswith("sec-websocket-key")][0].split(": ")[1]
    acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
    write_frame(c, {"type": "auth_required"})


def serve(script):
    """A fake HA that logs each connection in and hands it to script(conn, index)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    conns = []

    def run():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            handshake(c)
            read_frame(c)
            write_frame(c, {"type": "auth_ok"})
            read_frame(c)  # subscribe
            conns.append(c)
            threading.Thread(target=script, args=(c, len(conns) - 1), daemon=True).start()
    threading.Thread(target=run, daemon=True).start()
    return "ws://127.0.0.1:%d/core/websocket" % srv.getsockname()[1], srv, conns


def watch(url, callback, stop):
    t = threading.Thread(target=HA("http://x", url, "tok").watch_events, args=("t", callback, stop), daemon=True)
    t.start()
    return t


def wait_until(fn, seconds=5):
    import time
    end = time.time() + seconds
    while time.time() < end and not fn():
        time.sleep(0.02)
    return fn()


def test_a_failing_callback_does_not_end_the_watcher():
    def script(c, i):
        write_frame(c, event(1))
        write_frame(c, event(2))
    url, srv, _ = serve(script)
    got, stop = [], threading.Event()

    def cb(d):
        got.append(d["n"])
        if d["n"] == 1:
            raise RuntimeError("boom")
    t = watch(url, cb, stop)
    try:
        assert wait_until(lambda: 2 in got)
        assert t.is_alive()
    finally:
        stop.set()
        srv.close()


def test_a_silent_connection_gets_pings_and_stays_open_while_it_answers(monkeypatch):
    monkeypatch.setattr(HA, "IDLE", 0.2, raising=False)
    pings = []

    def script(c, i):
        try:
            while True:
                op, _ = read_any(c)
                if op == 9:
                    pings.append(i)
                    c.sendall(bytes([0x8A, 0]))  # pong
        except OSError:
            pass  # the test ended
    url, srv, conns = serve(script)
    stop = threading.Event()
    t = watch(url, lambda d: None, stop)
    try:
        assert wait_until(lambda: len(pings) >= 3)
        assert len(conns) == 1 and t.is_alive()  # an idle socket is normal: no reconnect
    finally:
        stop.set()
        srv.close()


def test_a_connection_that_does_not_answer_the_ping_is_replaced(monkeypatch):
    monkeypatch.setattr(HA, "IDLE", 0.2, raising=False)
    got = []

    def script(c, i):
        if i == 1:
            write_frame(c, event(7))
    url, srv, conns = serve(script)
    stop = threading.Event()
    watch(url, lambda d: got.append(d["n"]), stop)
    try:
        assert wait_until(lambda: got == [7], 10)
        assert len(conns) == 2
    finally:
        stop.set()
        srv.close()


def test_a_stall_in_the_auth_exchange_is_unavailable_not_a_timeout():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        c, _ = srv.accept()
        handshake(c)  # then silence: no auth_ok
        threading.Event().wait(2)
    threading.Thread(target=run, daemon=True).start()
    ha = HA("http://x", "ws://127.0.0.1:%d/core/websocket" % srv.getsockname()[1], "tok")
    try:
        with pytest.raises(HAUnavailable):
            ha._connect(timeout=0.3)
    finally:
        srv.close()


def test_command_turns_an_oserror_from_connect_into_haerror(monkeypatch):
    ha = HA("http://x", "ws://x", "tok")

    def broken(timeout=30):
        raise OSError("reset")
    monkeypatch.setattr(ha, "_connect", broken)
    with pytest.raises(HAError):
        ha.command("config/auth/list")
