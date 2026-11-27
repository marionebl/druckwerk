"""Hub app tests: the real server on a loopback port with a fake Home Assistant and a fake bridge, no network."""
import http.client
import importlib.util
import json
import os
import re
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
# HUB_IMPL lets the suite run against a stub dir to show it fails without the implementation.
for p in (os.environ.get("HUB_IMPL") or os.path.join(REPO, "hub"), os.path.join(REPO, "hub", "queue")):
    sys.path.insert(0, p)

from externals import HA_VENDOR_DOMAIN as VENDOR  # noqa: E402

SERIAL, SERIAL2 = "FAKE0001", "FAKE0002"
ADMIN, USER, OTHER = "u-admin", "u-user", "u-other"
USERS = [
    {"id": ADMIN, "name": "Ada", "is_owner": False, "is_active": True, "system_generated": False, "group_ids": ["system-admin"]},
    {"id": USER, "name": "Ben", "is_owner": False, "is_active": True, "system_generated": False, "group_ids": ["system-users"]},
    {"id": OTHER, "name": "Cy", "is_owner": False, "is_active": True, "system_generated": False, "group_ids": ["system-users"]},
    {"id": "sys", "name": "Supervisor", "is_owner": False, "is_active": True, "system_generated": True, "group_ids": []},
]
DEVICES = [{"id": "dev1", "identifiers": [[VENDOR, SERIAL]], "name": "h2s_" + SERIAL.lower(), "name_by_user": "Lab H2S", "model": "H2S"}]
ENTITIES = [{"entity_id": "sensor.lab_h2s_print_status", "device_id": "dev1", "platform": VENDOR, "unique_id": SERIAL + "_print_status"}]
SLICE_INFO = open(os.path.join(HERE, "fixtures", "slice_info.config")).read()


# A start G-code the guard accepts for the H2S: stage markers, AMS load, heated nozzle, then an extruding move.
GCODE = ("; printer_model = Bambu Lab H2S\n; EXECUTABLE_BLOCK_START\nM1002 gcode_claim_action : 4\nM620 S0A\nT0\nM621 S0A\n"
         "M109 S245\n; CHANGE_LAYER\nG1 X1 Y1 E5\n")


def make_3mf(path, slice_info=SLICE_INFO, gcode=GCODE):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", slice_info)
        z.writestr("Metadata/plate_1.gcode", gcode)
        z.writestr("Metadata/plate_1.png", b"\x89PNG\r\n\x1a\nfake")
    return str(path)


def st(state, **attrs):
    return {"state": state, "attributes": attrs}


class FakeHA:
    def __init__(self, vendor=True):
        """vendor=False is a Home Assistant without the vendor integration: no devices, no entities of it."""
        self.users, self.devices, self.entities = USERS, DEVICES if vendor else [], ENTITIES if vendor else []
        self.calls, self.fail = [], {}
        self.commands = []  # websocket commands that change HA: (type, fields)
        self.events = []  # bus events fired through the REST API: (type, data)
        self.entries = []  # config entries as HA lists them
        self.s = {"schedule.workday": st("on", friendly_name="Workday"), "light.lamp": st("on")}
        if vendor:
            self.s["sensor.lab_h2s_print_status"] = st("running")

    def state(self, eid):
        return (self.s.get(eid) or {}).get("state")

    def states(self, max_age=3):
        return dict({k: dict(v, entity_id=k) for k, v in self.s.items()})

    def command(self, type_, **kw):
        if type_ == "config_entries/get":
            if "config_entries/get" in self.fail:
                raise RuntimeError("websocket down")
            return [e for e in self.entries if e["domain"] == kw.get("domain", e["domain"])]
        if type_ == "input_boolean/create":
            self.commands.append((type_, kw))
            return dict(kw, id=re.sub(r"[^a-z0-9]+", "_", kw["name"].lower()).strip("_"))  # HA slugifies the name
        return {"config/auth/list": self.users, "config/device_registry/list": self.devices,
                "config/entity_registry/list": self.entities}[type_]

    def call_service(self, domain, service, data, response=False):
        self.calls.append((domain, service, data))
        if (domain, service) in self.fail:
            raise RuntimeError(self.fail[(domain, service)])
        if domain == "homeassistant":
            self.s[data["entity_id"]] = st("on" if service == "turn_on" else "off")
        return {}

    def fire_event(self, event_type, data):
        if "fire_event" in self.fail:
            raise RuntimeError(self.fail["fire_event"])
        self.events.append((event_type, data))

    def image(self, eid):
        return "image/jpeg", b"JPEGDATA"


DEFAULT_SPOOL = {"id": "", "brand": "", "type": "PLA", "name": "PLA Basic", "colour": "FFFFFF", "grams_left": 1000, "grams_total": 1000,
                 "printer": None, "unit": None, "slot": None, "in_printer": False, "rfid": None, "account": "a_x"}


def task(id, title, started, serial=SERIAL, status="finished", seconds=600, grams=5.0):
    """A normalized cloud task as the bridge's /api/tasks returns it."""
    return {"id": id, "serial": serial, "title": title, "status": status, "status_code": status, "started_at": started,
            "ended_at": started + seconds, "seconds": seconds, "grams": grams, "cover_url": "", "plate": 1, "design_title": ""}


# What the bridge's MQTT status says by default: the Lab H2S prints a cube (42 %, AMS 1 holds PETG and PLA), the P1S is idle.
DEFAULT_PRINT = {
    SERIAL: {"gcode_state": "RUNNING", "mc_percent": 42, "mc_remaining_time": 73, "layer_num": 10, "total_layer_num": 50,
             "nozzle_temper": 220, "nozzle_target_temper": 220, "bed_temper": 60, "bed_target_temper": 60,
             "device": {"ctc": {"info": {"temp": 30}}}, "stg_cur": 0, "subtask_name": "cube",
             "lights_report": [{"node": "chamber_light", "mode": "on"}],
             "hms": [{"attr": 0x03000100, "code": 0x00020007}],
             "ams": {"tray_now": "1", "ams": [{"id": "0", "humidity": "2", "temp": "28.5", "tray": [
                 {"id": "0", "state": 11, "tray_type": "PETG", "tray_sub_brands": "PETG HF", "tray_color": "ADB1B2FF", "remain": 80},
                 {"id": "1", "state": 11, "tray_type": "PLA", "tray_sub_brands": "PLA Basic", "tray_color": "BB3D43FF", "remain": 55}]}]}},
    SERIAL2: {"gcode_state": "IDLE", "stg_cur": -1},
}


class FakeBridge(BaseHTTPRequestHandler):
    log = []
    tasks = {}  # serial -> normalized tasks; a serial in task_fail answers 502
    task_fail = set()
    task_calls = []
    drying = []  # what GET /api/drying/<serial> answers
    drying_fail = {}  # path -> (status, error) for POSTs that the bridge refuses
    printer_extra = {}  # serial -> fields merged into GET /api/printers (print_status, live)
    status = {}  # serial -> the `print` object GET /api/status/<serial> answers (default: DEFAULT_PRINT)
    status_fail = set()  # serials whose status answers 503
    post_answer = {}  # path -> (status, body) instead of the usual answer
    library = {}  # account -> spools as the bridge's /api/filament lists them
    library_fail = None  # (status, error) that every /api/filament call answers
    library_calls = []  # (method, path, body)

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/health":
            return self.reply(200, {"server": "ok", "accounts": [{"id": "a_x", "email": "a@example.invalid", "logged_in": True,
                                                                  "daemon": {"running": True}}]})
        if self.path == "/api/printers":
            return self.reply(200, [dict(p, **FakeBridge.printer_extra.get(p["serial"], {})) for p in (
                {"serial": SERIAL, "name": "Lab", "dev_product_name": "H2S", "account": "a_x", "email": "a@example.invalid"},
                {"serial": SERIAL2, "name": "Other", "dev_product_name": "P1S", "account": "a_x"})])
        if self.path.startswith("/api/filament"):
            return self.filament("GET", None)
        if self.path.startswith("/api/tasks?"):
            serial = self.path.split("serial=")[1].split("&")[0]
            FakeBridge.task_calls.append(self.path)
            if serial in FakeBridge.task_fail:
                return self.reply(502, {"error": "Bambu cloud answered HTTP 500"})
            return self.reply(200, FakeBridge.tasks.get(serial, []))
        if self.path.startswith("/api/status/"):
            serial = self.path.rsplit("/", 1)[1]
            if serial in FakeBridge.status_fail:
                return self.reply(503, {"error": "no status from the printer"})
            return self.reply(200, {"print": FakeBridge.status.get(serial, DEFAULT_PRINT.get(serial, {}))})
        if self.path.startswith("/api/drying/"):
            return self.reply(200, FakeBridge.drying)
        if self.path.startswith("/api/camera/"):
            return self.reply(200, b"FRAME:" + self.path.encode(), "image/jpeg")
        self.reply(404, {"error": "no"})

    def filament(self, method, body):
        FakeBridge.library_calls.append((method, self.path, body))
        if FakeBridge.library_fail:
            return self.reply(FakeBridge.library_fail[0], {"error": FakeBridge.library_fail[1]})
        path, _, query = self.path.partition("?")
        account = query.split("account=")[1]
        spools = FakeBridge.library.setdefault(account, [])
        if method == "GET":
            return self.reply(200, spools)
        if method == "POST":
            spool = dict(DEFAULT_SPOOL, id=str(7000 + len(spools)), account=account, **body)
            spools.append(spool)
            return self.reply(200, spool)
        spool = next((x for x in spools if x["id"] == path.rsplit("/", 1)[1]), None)
        if method == "DELETE":
            spools.remove(spool)
            return self.reply(200, {"removed": spool["id"]})
        spool.update(body)
        self.reply(200, spool)

    def do_PUT(self):
        self.filament("PUT", json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"))

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        FakeBridge.log.append((self.path, body))
        if self.path.startswith("/api/filament"):
            return self.filament("POST", body)
        if self.path in FakeBridge.post_answer:
            code, answer = FakeBridge.post_answer[self.path]
            return self.reply(code, answer)
        if self.path == "/api/print":
            return self.reply(200, {"result": "sent"})
        if re.fullmatch(r"/api/(pause|light|rfid)/[A-Za-z0-9_-]+", self.path):
            return self.reply(200, {"sent": 1})
        if "/drying" in self.path and self.path in FakeBridge.drying_fail:
            code, text = FakeBridge.drying_fail[self.path]
            return self.reply(code, {"error": text})
        if re.fullmatch(r"/api/ams/[A-Za-z0-9_-]+/drying(/stop)?", self.path):
            return self.reply(200, {"sent": 1})
        if self.path == "/api/login/start":
            return self.reply(200, {"state": "need_code", "account": "a_x"})
        if self.path == "/api/login/code":
            return self.reply(200, {"state": "done", "account": "a_x"})
        self.reply(404, {"error": "no"})

    def do_DELETE(self):
        if self.path.startswith("/api/filament"):
            return self.filament("DELETE", None)
        self.reply(200, {"deleted": self.path.rsplit("/", 1)[1]})

    def log_message(self, *a):
        pass


class Hub:
    def __init__(self, tmp_path, vendor=True):
        spec = importlib.util.spec_from_file_location("slicer_server", os.path.join(REPO, "hub", "slicer", "server.py"))
        self.s = importlib.util.module_from_spec(spec)
        sys.modules["slicer_server"] = self.s
        spec.loader.exec_module(self.s)
        self.s.JOBS, self.s.SHARE = str(tmp_path / "jobs"), str(tmp_path / "share")
        self.tmp = tmp_path
        self.now = 1000.0
        self.ha = FakeHA(vendor)
        FakeBridge.log, FakeBridge.tasks, FakeBridge.task_fail, FakeBridge.task_calls = [], {}, set(), []
        FakeBridge.drying, FakeBridge.drying_fail = [], {}
        FakeBridge.printer_extra, FakeBridge.post_answer = {}, {}
        FakeBridge.status, FakeBridge.status_fail = {}, set()
        FakeBridge.library, FakeBridge.library_fail, FakeBridge.library_calls = {}, None, []
        self.bridge = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
        threading.Thread(target=self.bridge.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        from app import App
        self.app = App(self.ha, str(tmp_path / "data"), self.s.SHARE, ("127.0.0.1", self.bridge.server_address[1]), self.s,
                       lambda: "hub-host", 8099, clock=lambda: self.now, trusted_ips=["127.0.0.1"],
                       ui_dir=os.path.join(REPO, "hub", "ui"))
        media = tmp_path / "media"
        (media / "druckwerk").mkdir(parents=True)
        self.media = media
        self.app.media_roots = [os.path.realpath(media)]
        self.app.media_dir = str(media / "druckwerk")
        self.app.install()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.s.Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.port = self.server.server_address[1]

    def req(self, method, path, body=None, user=None, secret=False, headers=None, raw=None):
        h = dict(headers or {})
        if user:
            h.update({"X-Remote-User-Id": user, "X-Remote-User-Name": user, "X-Remote-User-Display-Name": user.upper()})
        if secret:
            h["X-Hub-Secret"] = self.app.secret
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request(method, path, data, h)
        r = c.getresponse()
        out = r.read()
        c.close()
        try:
            return r.status, json.loads(out)
        except ValueError:
            return r.status, out

    def set_print(self, serial=SERIAL, **fields):
        """Change what the bridge's status says of a printer (the MQTT field names)."""
        def merge(old, new):
            return dict(old, **{k: merge(old.get(k) or {}, v) if isinstance(v, dict) else v for k, v in new.items()})
        FakeBridge.status[serial] = merge(FakeBridge.status.get(serial) or DEFAULT_PRINT.get(serial, {}), fields)

    def seed_share(self):
        """a, b, c and d .gcode.3mf in the share: a start sends the real file, so tests that start jobs need them."""
        os.makedirs(self.s.SHARE, exist_ok=True)
        for name in "abcd":
            make_3mf(os.path.join(self.s.SHARE, name + ".gcode.3mf"))

    def sent(self):
        """The prints the bridge received, as their JSON bodies without the file."""
        return [dict(b, file_b64="...") for path, b in FakeBridge.log if path == "/api/print"]

    def setup_printers(self):
        self.app.registry.put(SERIAL, name="Lab", model="H2S", account="a_x", prefix="lab_h2s", electricity_kwh_price=0.4)
        self.app.registry.put(SERIAL2, name="Other", model="P1S", account="a_x", prefix="lab_p1s")

    def cloud_spool(self, account="a_x", **fields):
        """Put a spool into the fake bridge's cloud library (loaded in a tray when printer, unit and slot are given)."""
        spools = FakeBridge.library.setdefault(account, [])
        spool = dict(DEFAULT_SPOOL, id=str(5000 + len(spools)), account=account, in_printer=bool(fields.get("printer")), **fields)
        spools.append(spool)
        return spool

    def owner(self, user, printer=SERIAL):
        """Make a user the owner of a printer (a property of the printer, not a grant)."""
        self.app.registry.set_owner(printer, user)

    def grant(self, user, printer=SERIAL, **kw):
        return self.app.grants.add(user, printer, **kw)

    def sliced(self, owner=USER):
        """A finished slice job as the slicer app keeps it, with the real slice_info.config."""
        jid = "a1b2c3d4e5f6"
        d = self.tmp / "jobs" / jid
        d.mkdir(parents=True, exist_ok=True)
        result = make_3mf(d / "result.gcode.3mf")
        self.s.jobs[jid] = {"id": jid, "filename": "cube.stl", "model": str(d / "cube.stl"), "state": "done", "created": 1,
                            "printer": {"serial": SERIAL, "name": "Lab", "model": "H2S"}, "quality": "better", "result": result,
                            "owner_user_id": owner, "settings": {"tray": {"ams": "0", "id": "0"}}}
        return jid


