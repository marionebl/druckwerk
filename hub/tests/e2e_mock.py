#!/usr/bin/env python3
"""Fake bridge, Supervisor and Home Assistant for the image end-to-end test (hub/tests/ui_e2e.sh).

  python3 e2e_mock.py HTTP_PORT WS_PORT
HTTP_PORT: bridge API (/api/...), HA core REST (/core/...), Supervisor (/supervisor/...). WS_PORT: HA websocket.
All serials, names and entities are fake; the printers match the ones in hub/ui/mock_server.py."""
import base64
import hashlib
import json
import socket
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
H2S, P1S, X1C = "FAKE-H2S-0001", "FAKE-P1S-0002", "FAKE-X1C-0003"
PREFIX = {H2S: "h2s_fake0001", P1S: "p1s_fake0002", X1C: "x1c_fake0003"}
NAMES = {H2S: ("Workshop H2S", "H2S"), P1S: ("Office P1S", "P1S"), X1C: ("Garage X1C", "X1C")}
# A 1x1 JPEG, so camera <img> elements really decode.
JPEG = base64.b64decode("/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
                        "wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=")
# (type, colour, name, remain %, bridge tray_info_idx); None = empty slot
UNITS = [[("PLA", "F2F2F2", "Acme PLA White", 88, "GFA00"), ("PETG", "1F6FEB", "Prusament PETG Blue", 61, "GFG02"),
          ("PLA", "E4572E", "Sunlu PLA Red", 14, "GFA00"), None],
         [("TPU", "222222", "TPU 95A Black", 73, "GFU01")]]
P1S_TRAYS = [("PLA", "2E2E2E", "Generic PLA Black", 95, "GFA00"), ("PLA", "F2C94C", "Generic PLA Yellow", 40, "GFA00"), None, None]


def st(state, **attrs):
    return {"state": state, "attributes": attrs}


def mqtt_tray(i, t):
    """A tray as the printer's MQTT status reports it; state 10 is an empty slot, 11 one with a spool."""
    if not t:
        return {"id": str(i), "state": 10}
    return {"id": str(i), "state": 11, "tray_type": t[0], "tray_color": t[1] + "FF", "tray_info_idx": t[4], "tray_sub_brands": t[2], "remain": t[3]}


def mqtt_status(state, units, **kw):
    """The `print` object of the bridge's /api/status/<serial>; the hub reads everything about a printer from it."""
    nozzle, bed = kw.get("nozzle", 25), kw.get("bed", 25)
    return {"gcode_state": state, "stg_cur": kw.get("stage", -1), "mc_percent": kw.get("progress", 0), "mc_remaining_time": kw.get("remaining", 0),
            "layer_num": kw.get("layer", 0), "total_layer_num": kw.get("total", 0), "nozzle_temper": nozzle, "nozzle_target_temper": kw.get("nozzle_t", 0),
            "bed_temper": bed, "bed_target_temper": kw.get("bed_t", 0), "device": {"ctc": {"info": {"temp": 27}}},
            "subtask_name": kw.get("task", ""), "hms": kw.get("hms", []),
            "ams": {"tray_now": kw.get("tray_now", "255"), "ams": [
                {"id": str(u), "humidity": ("2", "4")[u], "temp": ("27", "31")[u], "tray": [mqtt_tray(i, t) for i, t in enumerate(trays)]}
                for u, trays in enumerate(units)]}}


def build_prints():
    return {H2S: mqtt_status("RUNNING", UNITS, stage=0, progress=42, remaining=95, layer=128, total=300, nozzle=249, nozzle_t=250, bed=60,
                             bed_t=60, task="Gridfinity bin 4x2", tray_now="1"),
            P1S: mqtt_status("IDLE", [P1S_TRAYS]),
            # paused for a missing filament, with the HMS error the printer reports for it
            X1C: mqtt_status("PAUSE", [], stage=6, progress=63, remaining=40, layer=190, total=300, nozzle=215, nozzle_t=220, bed=60, bed_t=60,
                             task="Cable clips", hms=[{"attr": 0x07000100, "code": 0x00020007}])}


def build_states():
    """Home Assistant has no entities of the vendor integration: only what the access rules and notifications use."""
    return {"schedule.workshop_hours": st("on", friendly_name="Workshop hours"), "input_boolean.guests_home": st("off", friendly_name="Guests home"),
            "person.lena": st("home", friendly_name="Lena")}


USERS = [{"id": "u-admin", "name": "Alex Admin", "is_owner": False, "is_active": True, "system_generated": False, "group_ids": ["system-admin"]},
         {"id": "u-user", "name": "Lena Tester", "is_owner": False, "is_active": True, "system_generated": False, "group_ids": ["system-users"]},
         {"id": "u-friend", "name": "Jonas Example", "is_owner": False, "is_active": True, "system_generated": False, "group_ids": ["system-users"]}]
STATES = build_states()
PRINTS = build_prints()
DEVICES, ENTITIES = [], []  # no printer devices or entities in HA
CALLS = []


def bridge_ams(trays):
    return {"ams": {"ams": [{"id": "0", "tray": [{"id": str(i), "tray_type": t[0], "tray_color": t[1] + "FF", "tray_info_idx": t[4],
                                                  "tray_sub_brands": t[2]} if t else {"id": str(i)} for i, t in enumerate(trays)]}]}, "vt_tray": {}}


SPOOLS = [{"id": str(5000 + i), "brand": n.split()[0], "type": t, "name": n, "colour": c, "grams_left": left, "grams_total": 1000,
           "printer": pr, "unit": u, "slot": s, "in_printer": pr is not None, "rfid": None, "account": "acc1"}
          for i, (t, c, n, left, pr, u, s) in enumerate([
              ("PLA", "F2F2F2", "Acme PLA White", 880, H2S, 0, 0), ("PETG", "1F6FEB", "Prusament PETG Blue", 610, H2S, 0, 1),
              ("PLA", "E4572E", "Sunlu PLA Red", 140, H2S, 0, 2), ("TPU", "222222", "TPU 95A Black", 730, H2S, 1, 0),
              ("PLA", "2E2E2E", "Generic PLA Black", 950, None, None, None)])]


class H(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/health":
            return self.reply(200, {"server": "ok", "accounts": [{"id": "acc1", "email": "owner@example.invalid", "logged_in": True,
                                                                  "daemon": {"running": True}}]})
        if p == "/api/printers":
            return self.reply(200, [{"dev_id": s, "serial": s, "name": n, "dev_product_name": m, "account": "acc1", "live": {"ok": 1}}
                                    for s, (n, m) in NAMES.items()])
        if p == "/api/filament":
            return self.reply(200, SPOOLS)
        if p.startswith("/api/ams/"):
            serial = p.rsplit("/", 1)[1]
            return self.reply(200, bridge_ams(UNITS[0] if serial == H2S else P1S_TRAYS))
        if p.startswith("/api/status/"):
            return self.reply(200, {"print": PRINTS.get(p.rsplit("/", 1)[1], {})})
        if p.startswith("/api/camera/"):
            return self.reply(200, JPEG, "image/jpeg")
        if p == "/calls":
            return self.reply(200, CALLS)
        if p == "/core/states":
            return self.reply(200, [dict(v, entity_id=k) for k, v in STATES.items()])
        if p.startswith("/core/states/"):
            eid = p.rsplit("/", 1)[1]
            return self.reply(200, dict(STATES[eid], entity_id=eid)) if eid in STATES else self.reply(404, {"message": "Entity not found"})
        self.reply(404, {"error": "no such route " + p})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        CALLS.append({"path": self.path, "body": body})
        if self.path == "/api/login/start":
            return self.reply(200, {"state": "need_code", "account": "acc1"})
        if self.path == "/api/login/code":
            return self.reply(200, {"state": "done", "account": "acc1"})
        if self.path == "/api/print":  # the app sends prints to the bridge itself
            return self.reply(200, {"result": "sent"})
        if self.path.startswith("/core/services/"):
            return self.reply(200, {"service_response": {"result": "sent"}})
        if self.path.split("?")[0] == "/api/filament":
            SPOOLS.append(dict(SPOOLS[-1], **body, id=str(5000 + len(SPOOLS)), printer=None, in_printer=False))
            return self.reply(200, SPOOLS[-1])
        self.reply(200, {})  # supervisor discovery

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        CALLS.append({"path": self.path, "body": body})
        sid = self.path.split("?")[0].rsplit("/", 1)[1]
        spool = next((s for s in SPOOLS if s["id"] == sid), None)
        if not spool:
            return self.reply(404, {"error": "unknown spool"})
        spool.update(body)
        self.reply(200, spool)

    def do_DELETE(self):
        self.reply(200, {"deleted": self.path.rsplit("/", 1)[1]})

    def log_message(self, *a):
        pass


def recv_exact(c, n):
    out = b""
    while len(out) < n:
        chunk = c.recv(n - len(out))
        if not chunk:
            raise OSError("closed")
        out += chunk
    return out


def read_frame(c):
    b0, b1 = recv_exact(c, 2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", recv_exact(c, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", recv_exact(c, 8))[0]
    mask = recv_exact(c, 4)
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(recv_exact(c, n)))
    return None if b0 & 0x0F == 8 else json.loads(data) if data else {}


def write_frame(c, obj):
    data = json.dumps(obj).encode()
    n = len(data)
    head = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n))
    c.sendall(head + data)


def ws_client(c):
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(1024)
        key = [l for l in req.decode().split("\r\n") if l.lower().startswith("sec-websocket-key")][0].split(": ")[1]
        acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
        write_frame(c, {"type": "auth_required"})
        read_frame(c)
        write_frame(c, {"type": "auth_ok"})
        while (m := read_frame(c)) is not None:
            result = {"config/auth/list": USERS, "config/device_registry/list": DEVICES, "config/entity_registry/list": ENTITIES}.get(m["type"])
            write_frame(c, {"id": m["id"], "type": "result", "success": True, "result": result})
    except OSError:
        pass
    finally:
        c.close()


def serve_ws(port):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=ws_client, args=(c,), daemon=True).start()


if __name__ == "__main__":
    threading.Thread(target=serve_ws, args=(int(sys.argv[2]),), daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
