"""Fake cloud login endpoints. Accounts: direct@ (password only), tfa@ (password then tfa), anything else
(password, then email code). Password "pw", email code "123456", tfa code "654321".
Every login issues new tokens (server.tokens: latest per email, server.issued: all) and each email has its own printers."""
import json
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import externals  # noqa: E402

DEVICES = {
    "a@example.com": [
        {"dev_id": "SERIAL001", "name": "Office P1S", "dev_product_name": "P1S", "dev_access_code": "ACCESS-1234",
         "online": True},
        {"dev_id": "SERIAL002", "name": "Office P1S", "dev_product_name": "A1", "dev_access_code": "ACCESS-5678"}],
    "b@example.com": [
        {"dev_id": "SERIALB01", "name": "Garage H2S", "dev_product_name": "H2S", "dev_access_code": "ACCESS-9999"}],
}


def TASKS(serial, email):
    """The cloud's task history as the vendor app's list shows it; the title names the account so tests see which token was used."""
    tag = re.sub(r"\W", "", email)
    return [
        {"id": 103, "title": "", "designTitle": "Vase-" + tag, "deviceId": serial, "deviceName": "Office", "status": 4,
         "startTime": "2026-11-27T12:00:00Z", "endTime": None, "costTime": 0, "weight": 0, "cover": "", "plateIndex": 1, "mode": "local"},
        {"id": 102, "title": "cube-" + tag, "designTitle": "Cube", "deviceId": serial, "deviceName": "Office", "status": "failed",
         "startTime": "2026-11-27T10:00:00Z", "endTime": "2026-11-27T10:30:00Z", "costTime": 1800, "weight": 4.5,
         "cover": "https://cdn.example/c.png", "plateIndex": 2, "mode": "cloud_file",
         "modelId": "US1", "profileId": 777, "designId": 5},
        {"id": 101, "title": "old", "designTitle": "", "deviceId": serial, "deviceName": "Office", "status": 2,
         "startTime": "2026-11-26T10:00:00Z", "endTime": "2026-11-26T11:00:00Z", "costTime": 3600, "weight": 12.25,
         "cover": "", "plateIndex": 1, "mode": "cloud_file"}]


FILAMENT = "/v1/design-user-service/my/filament/v2"


def SPOOLS(email):
    """The account's filament library: 60 spools, more than the bridge's page of 50, so paging is exercised."""
    tag = re.sub(r"\W", "", email)
    out = [{"id": 9000 + i, "filamentVendor": "Acme", "filamentType": "PLA", "filamentName": "PLA Basic " + tag, "filamentId": "GFA00",
            "color": "FF000000" if i == 0 else "00FF00", "colorType": 2, "netWeight": 500 + i, "totalNetWeight": 1000,
            "devId": None, "amsId": None, "slotId": "", "inPrinter": False, "createType": "manual"} for i in range(60)]
    out[1].update(devId="SERIAL001", amsId=0, slotId="2", inPrinter=True, createType="ams", RFID="RF1", netWeight=None, totalNetWeight=None)
    return out


def devices(email):
    return DEVICES.get(email, [{"dev_id": "SER-" + re.sub(r"\W", "", email), "name": "Default"}])


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def issue(self, email):
        srv = self.server
        n, tag = 1 + sum(1 for e, _, _ in srv.issued if e == email), re.sub(r"\W", "", email)
        token, refresh = "TOKEN-SECRET-%s-%d" % (tag, n), "REFRESH-SECRET-%s-%d" % (tag, n)
        srv.issued.append((email, token, refresh))
        srv.tokens[email] = (token, refresh)
        srv.owner[token] = email
        self.reply(200, {"accessToken": token, "refreshToken": refresh})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.server.calls.append((self.path, body, self.headers.get(externals.CLOUD_CLIENT_HEADER)))
        if self.path == "/v1/user-service/user/login":
            time.sleep(self.server.login_delay)
            acct = body.get("account", "")
            if "password" in body:
                if body["password"] != "pw":
                    return self.reply(400, {"error": "bad password"})
                if acct.startswith("direct@"):
                    return self.issue(acct)
                if acct.startswith("tfa@"):
                    return self.reply(200, {"tfaKey": "tfa-key-" + acct})
                return self.reply(200, {"loginType": "verifyCode"})
            return self.issue(acct) if body.get("code") == "123456" else self.reply(400, {"error": "bad code"})
        if self.path == FILAMENT:
            email = self.server.owner.get(self.headers.get("Authorization", "")[7:])
            if not email:
                return self.reply(401, {})
            rec = dict(body, id=9100 + len(self.server.filaments.setdefault(email, SPOOLS(email))))
            self.server.filaments[email].append(rec)
            return self.reply(200, rec)
        if self.path == "/v1/user-service/user/sendemail/code":
            # The real cloud answers 200 with an empty body here (seen live on 2026-11-21).
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/v1/user-service/user/tfa":
            key = body.get("tfaKey", "")
            if key.startswith("tfa-key-") and body.get("tfaCode") == "654321":
                return self.issue(key[8:])
            return self.reply(400, {"error": "bad tfa"})
        self.reply(404, {})

    def do_GET(self):
        email = self.server.owner.get(self.headers.get("Authorization", "")[7:])
        if not email:
            return self.reply(401, {})
        if self.path == "/v1/design-user-service/my/preference":
            return self.reply(200, {"uid": 4242, "name": "Tester"})
        if self.path == "/v1/iot-service/api/user/bind":
            return self.reply(200, {"devices": devices(email)})
        if urlsplit(self.path).path == FILAMENT:
            self.server.filament_calls.append((self.path, email))
            q = parse_qs(urlsplit(self.path).query)
            lib = self.server.filaments.setdefault(email, SPOOLS(email))
            page = lib[int(q["offset"][0]):int(q["offset"][0]) + int(q["limit"][0])]
            shape = self.server.list_shape
            return self.reply(200, page if shape == "array" else {"data": {"hits": page}} if shape == "data" else {"hits": page, "total": len(lib)})
        if self.path.startswith("/v1/user-service/my/tasks?"):
            self.server.task_calls.append((self.path, email))
            q = parse_qs(urlsplit(self.path).query)
            serial, limit = q["deviceId"][0], int(q["limit"][0])
            if serial == "SERIAL002":
                return self.reply(500, {"error": "cloud broke"})
            return self.reply(200, {"total": 3, "hits": TASKS(serial, email)[:limit]})
        if self.path.startswith("/v1/iot-service/api/user/profile/"):
            self.server.task_calls.append((self.path, email))
            if self.path != "/v1/iot-service/api/user/profile/777?model_id=US1":
                return self.reply(404, {})
            return self.reply(200, {"url": "https://cdn.example/file.gcode.3mf"})
        self.reply(404, {})

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        email = self.server.owner.get(self.headers.get("Authorization", "")[7:])
        self.server.calls.append((self.path, body, "PUT"))
        if not email:
            return self.reply(401, {})
        if self.path != FILAMENT:  # the id belongs in the body
            return self.reply(404, {})
        rec = next((r for r in self.server.filaments.get(email, []) if r["id"] == body.get("id")), None)
        if not rec:
            return self.reply(404, {})
        rec.update(body)
        self.reply(200, rec)

    def do_DELETE(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        email = self.server.owner.get(self.headers.get("Authorization", "")[7:])
        self.server.calls.append((self.path, body, "DELETE"))
        if not email:
            return self.reply(401, {})
        if self.path != FILAMENT + "/batch":
            return self.reply(404, {})
        lib = self.server.filaments.setdefault(email, SPOOLS(email))
        lib[:] = [r for r in lib if r["id"] not in body.get("ids", [])]
        self.reply(200, {})

    def log_message(self, *a):
        pass


def start():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.calls, server.issued, server.tokens, server.owner = [], [], {}, {}
    server.login_delay = 0  # seconds the login endpoint waits before it answers
    server.task_calls, server.filament_calls, server.filaments, server.list_shape = [], [], {}, "hits"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
