"""Routes and background work of the Druckwerk app (contract: hub/API.md).

Mounted onto the slicer server (hub/slicer/server.py) by hub.py: install() adds the routes, puts an identity check in front of
every one of them and adds PUT/DELETE dispatch. Home Assistant I/O goes through the `ha` object (hub/ha.py; fakes in tests).
"""
import collections
import hashlib
import hmac
import http.client
import io
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.request
import zipfile
from urllib.parse import parse_qs, quote, unquote, urlsplit

import actions
import externals
import files
import filament
import history
from httplen import content_length
import nozzles
import printer_state
import progress
import slice_settings
from gcode_guard import file_nozzle
from ha import HAError, HAUnavailable
from queue_access import Access, Caller, Forbidden, Grants, Unauthorized, caller_from_headers
from queue_engine import WAITING, NotFound, Queue, QueueError
from queue_registry import Registry, check_nozzles
from integration import JPEG_PRODUCTS, Integration
from notify import Notifier
from sender import Sender
from watchdog import Watchdog

HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "server", "date"}
SECRET_HEADER = "X-Hub-Secret"
ACT_AS_HEADER = "X-Druckwerk-Act-As"
IDENTITY_HEADERS = ("X-Remote-User-Id", "X-Remote-User-Name", "X-Remote-User-Display-Name")
JOB_ID = r"([0-9a-f]{12})"
SERIAL = r"([A-Za-z0-9_-]{1,40})"
OFFLINE_JPEG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "offline.jpg")
# Entity ids the queue and the watchdog ask for: <prefix>_<suffix>. State() answers them from the bridge, so they need not
# exist in Home Assistant (with the vendor integration installed they are its real ids).
SENSORS = {"status": "print_status", "stage": "current_stage", "progress": "print_progress", "tray": "active_tray"}
UI_FILES = r"(index\.html|style\.css|app\.js|api\.js|util\.js|feedback\.js|views/[a-z]+\.js|vendor/three\.module\.min\.js|vendor/mpegts\.js)"
# A feedback post carries screenshots; every other route keeps the slicer's 8 KB JSON limit.
FEEDBACK_MAX = 1500000
MIME = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8", "js": "text/javascript; charset=utf-8"}
MEDIA_ROOTS = ("/media", "/share")  # where a queue file may be named by absolute path (config.yaml maps media read-write)
MEDIA_DIR = "/media/druckwerk"  # snapshots and plate images of the actions
SLICED = re.compile(r"Metadata/plate_\d+\.gcode")
TASKS_TTL = 60  # seconds a printer's cloud task list is reused
SPOOLS_TTL = 30  # seconds an account's cloud filament library is reused
STALE_AFTER = 60  # seconds after which a printer view from an earlier status call is marked stale
STATUS_STALE_MAX = 600  # seconds the queue may still act on such a view
STATUS_CACHE_EVERY = 30  # seconds between writes of a printer's last good view to the data dir
CLOUD_OFFLINE_AFTER = 60  # seconds the cloud flag must say offline, reading after reading, before the page does
CLOUD_READING_GAP = 30  # a longer pause between two offline readings starts the count again
COVER_MAX = 5 * 1024 * 1024  # bytes of a cloud cover image
FILE_MAX = 400 * 1024 * 1024  # bytes of a sliced 3MF fetched from the cloud
MISS_TTL = 600  # seconds before a failed cloud file fetch is tried again
DOMAIN_DEFAULTS = "schedule,input_boolean,binary_sensor,person"


def log(msg):
    print("druckwerk: " + msg, file=sys.stderr, flush=True)


HttpError = actions.ActionError  # one error type for a failure with an HTTP status


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


def num(v, cast=float):
    try:
        return cast(float(v))
    except (TypeError, ValueError):
        return None


def fetch_image(url):
    """(bytes, content type) of an image at an http(s) URL; a failure is a 502."""
    if urlsplit(url).scheme not in ("http", "https"):
        raise HttpError(502, "cover image: not a web address")
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data, ctype = r.read(COVER_MAX + 1), r.headers.get_content_type()
    except (OSError, ValueError) as e:
        raise HttpError(502, "cover image: %s" % e)
    if len(data) > COVER_MAX or not ctype.startswith("image/"):
        raise HttpError(502, "cover image: not an image or too large")
    return data, ctype


def fetch_file(url, dest):
    """Download an http(s) URL to dest; a failure or a file over FILE_MAX is a 502 and leaves nothing."""
    if urlsplit(url).scheme not in ("http", "https"):
        raise HttpError(502, "cloud file: not a web address")
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as out:
            size = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > FILE_MAX:
                    raise HttpError(502, "cloud file: too large")
                out.write(chunk)
        os.replace(part, dest)
    except (OSError, ValueError) as e:
        raise HttpError(502, "cloud file: %s" % e)
    finally:
        if os.path.exists(part):
            os.remove(part)


class Capture:
    """Stands in for the request handler so a slicer handler's answer can be post-processed."""

    def __init__(self, h):
        self.h, self.out = h, None

    def __getattr__(self, name):
        return getattr(self.h, name)

    def send(self, status, body, ctype="application/json"):
        self.out = (status, body, ctype)


class Drying:
    """The queue's view of the bridge's AMS drying routes."""

    def __init__(self, app):
        self.app = app

    def start(self, serial, d):
        try:
            self.app.bridge_ok("POST", "/api/ams/%s/drying" % serial, d, timeout=30)
        except HttpError as e:
            raise (ValueError if e.status == 400 else RuntimeError)(str(e))

    def stop(self, serial, ams_id):
        self.app.bridge_ok("POST", "/api/ams/%s/drying/stop" % serial, {"ams_id": ams_id}, timeout=30)

    def state(self, serial, ams_id):
        unit = next((u for u in self.app.drying(serial, max_age=0) or [] if u["ams_id"] == ams_id), None)
        return {"active": unit["active"], "left_min": unit["left_min"]} if unit else None


class App:
    def __init__(self, ha, data_dir, share, bridge, slicer, hostname_fn, port, clock=time.time,
                 trusted_ips=(), ui_dir=None):
        self.ha, self.share, self.bridge, self.s = ha, share, bridge, slicer
        self.hostname_fn, self.port, self.clock, self.trusted_ips = hostname_fn, port, clock, set(trusted_ips)
        self.data_dir, self.ui_dir = data_dir, ui_dir
        os.makedirs(os.path.join(data_dir, "plates"), exist_ok=True)
        self.registry = Registry(data_dir)
        self.prices = filament.Prices(data_dir)
        self.loads = filament.LoadRequests(data_dir)
        self.nozzle_requests = nozzles.NozzleRequests(data_dir)
        self.grants = Grants(self.registry, data_dir, self._grant_state, clock)
        self.access = Access(self.registry, self.grants, self.get_user)
        self.queue = Queue(self.registry, data_dir, self.state, self._call_service, self.get_trays, clock=clock,
                           job_allowed=self.access.job_allowed, dry=Drying(self), send_print=self.send_print,
                           get_nozzle=self.installed_diameter, get_requests=self.job_requests)
        self.plates_inbox = None  # set by hub.py when the app has a plates token
        self.media_roots = list(MEDIA_ROOTS)
        self.media_dir = MEDIA_DIR
        self.sender, self.watchdog, self.notifier, self.integration = Sender(self), Watchdog(self), Notifier(self), Integration(self)
        self.secret = self._load_secret()
        self._auto_at = None
        self._cache = {}
        self.cloud_miss = {}  # task id -> (time, reason) of a failed cloud file fetch
        self._cache_lock = threading.Lock()
        self._cloud_false = {}  # serial -> (first, latest) reading time of an unbroken run of offline readings
        self._saved_at = {}  # serial -> when its view was last written to status_cache.json
        self._load_views()
        # Events: seq starts at the boot time so it keeps growing across restarts (pollers hold the last seq they saw).
        self.seq = int(clock())
        self.events = collections.deque(maxlen=500)
        self.sync_lock = threading.RLock()
        self.known = self._states_of_jobs()
        self.waiting = self._waiting_ids()

    # -- identity --
    def _load_secret(self):
        path = os.path.join(self.data_dir, "hub.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)["secret"]
        secret = secrets.token_urlsafe(32)
        from queue_registry import write_json
        write_json(path, {"secret": secret})
        return secret

    def is_internal(self, h):
        given = h.headers.get(SECRET_HEADER) or ""
        return bool(given) and hmac.compare_digest(given.encode(), self.secret.encode())

    def caller(self, h):
        internal = self.is_internal(h)
        caller = caller_from_headers(h.headers, internal)
        act_as = (h.headers.get(ACT_AS_HEADER) or "").strip()
        if not (internal and act_as):
            return caller  # the header counts only with the secret; without it the caller stays admin (automations)
        user = self.get_user(act_as)
        if not user:
            raise Forbidden("rule: act-as user %s is not an active Home Assistant user" % act_as)
        return Caller(act_as, user["name"], user["name"])  # not internal: this user's rules apply

    def _cached(self, key, ttl, fn):
        """fn() result for ttl seconds; on failure the last good value, else the error."""
        with self._cache_lock:
            at, val = self._cache.get(key, (0, None))
        if self.clock() - at < ttl and val is not None:
            return val
        try:
            val = fn()
        except (HAError, HttpError):
            if val is None:
                raise
            return val
        with self._cache_lock:
            self._cache[key] = (self.clock(), val)
        return val

    def get_user(self, user_id):
        try:
            users = self._cached("users", 30, lambda: self.ha.command("config/auth/list"))
        except HAError:
            return None  # unknown means no admin rights
        u = next((u for u in users if u["id"] == user_id), None)
        if not u or not u.get("is_active", True):
            return None
        return {"name": u.get("name") or "", "is_admin": bool(u.get("is_owner") or "system-admin" in u.get("group_ids", []))}

    def find_user(self, ref):
        """(id, name) of the active, non-system HA user whose id or name is `ref` (name: any case), or None."""
        ref = str(ref or "").strip().lower()
        try:
            users = self._cached("users", 30, lambda: self.ha.command("config/auth/list"))
        except HAError:
            return None
        u = next((u for u in users if u.get("is_active", True) and not u.get("system_generated")
                  and ref in (u["id"].lower(), (u.get("name") or "").lower())), None) if ref else None
        return (u["id"], u.get("name") or "") if u else None

    def _grant_state(self, entity_id):
        try:
            s = self.ha.states().get(entity_id)
        except HAError:
            return None  # a condition that cannot be read does not grant
        return s["state"] if s else None

    # -- HA registries --
    def registries(self):
        def load():
            return (self.ha.command("config/device_registry/list"), self.ha.command("config/entity_registry/list"))
        try:
            return self._cached("registries", 60, load)
        except HAError:
            return [], []

    def suggested_prefix(self, serial):
        devices, entities = self.registries()
        dev = next((d for d in devices if [externals.HA_VENDOR_DOMAIN, serial] in [list(i) for i in d.get("identifiers", [])]), None)
        if not dev:
            return None
        main = next((e for e in entities if e.get("device_id") == dev["id"] and e.get("unique_id") == serial + "_print_status"), None)
        if main:
            return main["entity_id"].split(".", 1)[1].removesuffix("_print_status")
        return slug(dev.get("name_by_user") or dev.get("name") or "") or None

    def default_prefix(self, serial, name):
        """The vendor integration's entity prefix when it has the printer, else one made from the name (unique per printer)."""
        prefix = self.suggested_prefix(serial)
        if prefix:
            return prefix
        base = slug(name) or slug(serial)
        taken = {p["prefix"] for p in self.registry.all() if p["serial"] != serial}
        return base if base not in taken else "%s_%s" % (base, slug(serial))

    def ams(self, serial, raw=False, status=None):
        """AMS units of one printer, from the bridge: [{unit, humidity_index, temp, trays: [...]}]."""
        status = status or self.printer_status(serial) or {}
        library = self.library((self.registry.get(serial) or {}).get("account"), fetch=False)
        out = []
        for u in status.get("ams", []):
            trays = []
            for t in u["trays"]:
                spool = filament.in_tray(library, serial, u["unit"], t["slot"])
                trays.append({**{k: v for k, v in t.items() if k != "uid" or raw}, "spool_id": spool["id"] if spool else None})
            out.append(dict(u, trays=trays))
        return out

    def get_trays(self, serial):
        return [{"type": t["type"], "colour": "#" + t["colour"]} for a in self.ams(serial) for t in a["trays"] if t["type"]]

    # -- calls to the bridge and HA --
    def bridge_json(self, method, path, body=None, timeout=30):
        conn = http.client.HTTPConnection(*self.bridge, timeout=timeout)
        try:
            conn.request(method, path, json.dumps(body).encode() if body is not None else None,
                         {"Content-Type": "application/json"})
            r = conn.getresponse()
            raw = r.read()
        except OSError as e:
            raise HttpError(502, "bridge is not reachable: %s" % e)
        finally:
            conn.close()
        try:
            data = json.loads(raw or b"null")
        except ValueError:
            data = {"error": raw.decode(errors="replace")[:300]}
        return r.status, data

    def bridge_ok(self, method, path, body=None, timeout=30):
        status, data = self.bridge_json(method, path, body, timeout)
        if status != 200:
            raise HttpError(status if status in (400, 404, 409) else 502,
                            (data or {}).get("error", "bridge answered %s" % status) if isinstance(data, dict) else str(data))
        return data

    def proxy(self, h, path, query=""):
        """Stream a bridge answer to the client (the camera stream has no length)."""
        conn = http.client.HTTPConnection(*self.bridge, timeout=300)
        try:
            conn.request(h.command, path + ("?" + query if query else ""),
                         h.rfile.read(content_length(h.headers)) or None,
                         {k: v for k, v in h.headers.items() if k.lower() in ("content-type",)})
            r = conn.getresponse()
        except OSError as e:
            return h.send(502, {"error": "bridge is not reachable: %s" % e})
        h.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in HOP:
                h.send_header(k, v)
        h.close_connection = True  # HTTP/1.0 style: the body ends when we close
        h.end_headers()
        try:
            while chunk := r.read(65536):
                h.wfile.write(chunk)
        except OSError:
            pass  # viewer left
        finally:
            conn.close()

    def _call_service(self, domain, service, data):
        return self.ha.call_service(domain, service, data)

    def send_print(self, data):
        """The queue's print: no Home Assistant round trip. A file name is a file in the share."""
        data = dict(data)
        if not os.path.isabs(data["file"]):
            data["file"] = os.path.join(self.share, data["file"])
        return self.sender.print_file(data)

    def bridge_printers(self):
        """The bridge's printers with their live state; empty when it cannot say."""
        try:
            return self.bridge_ok("GET", "/api/printers", timeout=10)
        except HttpError:
            return []

    def ha_users(self):
        users = self.ha.command("config/auth/list")
        return [{"id": u["id"], "name": u.get("name") or "",
                 "is_admin": bool(u.get("is_owner") or "system-admin" in u.get("group_ids", []))}
                for u in users if u.get("is_active", True) and not u.get("system_generated")]

    # -- printers --
    def entity(self, p, key):
        return "sensor.%s_%s" % (p["prefix"], SENSORS[key])

    def installed_nozzle(self, p):
        """The nozzle in the printer ({"diameter", "type"}), read from its sensors every time; None when they cannot say."""
        try:
            return nozzles.installed(self.ha.states(), p["prefix"])
        except HAError:
            return None

    def installed_diameter(self, serial):
        p = self.registry.get(serial)
        now = p and self.installed_nozzle(p)
        return now["diameter"] if now else None

    def bridge_print(self, serial):
        try:
            status, data = self.bridge_json("GET", "/api/status/" + serial, timeout=10)
        except HttpError:
            return {}
        return (data or {}).get("print") or {} if status == 200 and isinstance(data, dict) else {}

    def _load_views(self):
        """Last good views from the data dir with their real time, so after a restart the age stays honest."""
        try:
            with open(os.path.join(self.data_dir, "status_cache.json")) as f:
                saved = json.load(f)
            for serial, e in saved.items():
                self._cache["view:" + serial] = (float(e["at"]), e["view"])
                self._saved_at[serial] = float(e["at"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass  # no file or a damaged one: start empty

    def _save_views(self, serial):
        now = self.clock()
        if now - self._saved_at.get(serial, 0) < STATUS_CACHE_EVERY:
            return
        self._saved_at[serial] = now
        from queue_registry import write_json
        with self._cache_lock:
            out = {k[5:]: {"at": at, "view": v} for k, (at, v) in self._cache.items() if k.startswith("view:")}
        try:
            write_json(os.path.join(self.data_dir, "status_cache.json"), out)
        except OSError as e:
            log("cannot write status_cache.json: %s" % e)

    def status_entry(self, serial):
        """(view, age in s) of the printer: fresh from the bridge, else the last good one (one failed call is not offline)."""
        v = printer_state.view(self.bridge_print(serial), self.clock())
        with self._cache_lock:
            if v is not None:
                self._cache["view:" + serial] = (self.clock(), v)
            at, last = self._cache.get("view:" + serial, (0, None))
        if v is not None:
            self._save_views(serial)
        return (last, self.clock() - at) if last is not None else (None, 0)

    def printer_status(self, serial):
        """What the printer reports now (hub/printer_state.py), from the bridge; None when the bridge cannot say.
        The queue acts on it, so a last good view counts for 10 min only."""
        v, age = self.status_entry(serial)
        return v if age <= STATUS_STALE_MAX else None

    def cloud_online(self, serial):
        """False only when the bridge's printer list has said offline, reading after reading, for CLOUD_OFFLINE_AFTER s;
        one reading, a gap or unknown counts as online."""
        try:
            printers = self._cached("printers", 5, self.bridge_printers)
        except (HAError, HttpError):
            return True
        flag = next((p.get("online") for p in printers if p.get("serial") == serial), None)
        with self._cache_lock:
            read = self._cache.get("printers", (self.clock(), None))[0]  # time of the reading, not of this call
            if flag is not False:
                self._cloud_false.pop(serial, None)
                return True
            first, last = self._cloud_false.get(serial, (read, read))
            if read - last > CLOUD_READING_GAP:
                first = read
            self._cloud_false[serial] = (first, max(read, last))
            return max(read, last) - first < CLOUD_OFFLINE_AFTER

    def state(self, entity_id):
        """The queue's state lookup: a registered printer's own sensors come from the bridge, any other entity from HA."""
        for p in self.registry.all():
            for key in SENSORS:
                if entity_id == self.entity(p, key):
                    v = self.printer_status(p["serial"])
                    if v is None:
                        return None
                    if key == "tray":
                        return v["active_tray"]["name"] if v["active_tray"] else "none"
                    value = v[{"status": "status", "stage": "stage", "progress": "progress"}[key]]
                    return None if value is None else str(value)
        return self.ha.state(entity_id)

    def drying(self, serial, max_age=5):
        """Units with a dryer, from the bridge; None when it cannot say."""
        try:
            return self._cached("dry:" + serial, max_age, lambda: self.bridge_ok("GET", "/api/drying/" + serial, timeout=10))
        except HttpError:
            return None

    def start_steps(self, serial, v, job):
        """(steps, job id) of a print start under way (hub/progress.py); the id is None when nobody started it here."""
        sending = self.sender.sending.get(serial)
        upload = None
        if sending:
            try:
                status, data = self.bridge_json("GET", "/api/print-progress/" + serial, timeout=3)
                upload = data if status == 200 and isinstance(data, dict) else None
            except HttpError:
                pass
            if not job:  # the job is still queued until the bridge answers
                job = next((j for j in self.queue.jobs if j["state"] == "queued" and os.path.basename(j.get("file") or "") == sending), None)
        steps = progress.steps(sending, upload, v)
        return steps, job["id"] if steps and job else None

    def printer_view(self, p, role):
        serial = p["serial"]
        v, age = self.status_entry(serial)
        seen = v is not None
        if seen and (age > STATUS_STALE_MAX or not self.cloud_online(serial)):
            v = None
        b = v or {}
        # null: no view was ever seen and nothing says offline (fresh start); the page waits instead of calling it offline
        online = None if not seen and self.cloud_online(serial) else v is not None and b["status"] != "offline"
        tray = b.get("active_tray")
        job = next((j for j in self.queue.jobs if j.get("printer") == serial and j["state"] in ("starting", "printing")), None)
        start, start_job = self.start_steps(serial, v, job)
        now = self.installed_nozzle(p)
        return {
            "serial": serial, "name": p["name"], "model": p["model"], "role": role,
            "online": online,
            "stale": v is not None and age > STALE_AFTER, "status_age": round(age) if v is not None else None,
            "status": b.get("status") or ("connecting" if online is None else None), "stage": b.get("stage"), "progress": b.get("progress"),
            "remaining_min": b.get("remaining_min"), "end_time": b.get("end_time"),
            "layer": b.get("layer"), "total_layers": b.get("total_layers"),
            "nozzle_temp": b.get("nozzle_temp"), "nozzle_target": b.get("nozzle_target"),
            "bed_temp": b.get("bed_temp"), "bed_target": b.get("bed_target"), "chamber_temp": b.get("chamber_temp"),
            "task_name": b.get("task_name"),
            "cover_url": "api/printers/%s/cover" % serial if b.get("task_name") else None,
            "camera_url": "api/printers/%s/camera" % serial, "account": p.get("account"),
            "camera_live": "mjpeg" if str(p["model"]).startswith(JPEG_PRODUCTS) else "mpegts",
            "light": self.integration.light(v),
            "active_tray": {"unit": tray["unit"], "slot": tray["slot"]} if tray else None,
            "drying": self.drying(serial) or [],
            "ams": self.ams(serial, status=v), "external": b.get("external"), "hms": b.get("hms", []),
            "current_job": dict(job) if job else None,
            "start": start, "start_job": start_job,
            "nozzle": now, "nozzles": nozzles.library(p, now),
            "queue_length": sum(1 for j in self.queue.jobs if j["state"] in WAITING and serial in self.access.serials_of(j)),
        }

    # -- grams, cost, events --
    def _states_of_jobs(self):
        return {j["id"]: j["state"] for j in self.queue.jobs + self.queue.history}

    def _waiting_ids(self):
        return [j["id"] for j in self.queue.jobs if j["state"] in WAITING]

    def emit(self, type_, serial, job, **extra):
        self.seq += 1
        # job always carries id, file, owner_name, target and printer (None until a printer took it).
        event = {"seq": self.seq, "type": type_, "serial": serial or "", "job": dict({"printer": None}, **job) if job else None, **extra}
        self.events.append(event)
        self.notifier.put(event)

    def sync(self):
        """Turn queue changes into events, and settle a finished job's spools and cost exactly once."""
        with self.sync_lock, self.queue.lock:
            now = self._states_of_jobs()
            changed = now != self.known or self._waiting_ids() != self.waiting
            for j in self.queue.jobs + self.queue.history:
                before, state = self.known.get(j["id"]), j["state"]
                if before == state:
                    continue
                serial = j.get("printer") or (j["target"] if j["target"] != "any" else "")
                if state == "pending_approval":
                    self.emit("approval_needed", serial, j)
                elif j.get("kind") == "dry":
                    continue  # a drying has no print events, spools or cost
                elif state == "starting":
                    self.emit("job_started", serial, j)
                elif state == "done":
                    self.settle(j)
                    self.emit("job_done", serial, j)
                elif state == "failed" and before in ("starting", "printing"):
                    self.emit("job_failed", serial, j)
            if changed:
                self.emit("queue_changed", "", None)
                self.queue._save()
            self.known, self.waiting = now, self._waiting_ids()

    def settle(self, job):
        """Take the grams off the spools in use and price the job."""
        serial = job.get("printer")
        printer = self.registry.get(serial) or {}
        grams = job.get("filament_grams") or []
        hint = tuple(job["slice_tray"]) if job.get("slice_tray") and len(job["filaments"]) == 1 else None
        prices, used = [], []
        library = self.printer_spools(serial)
        for f, g in zip(job["filaments"], grams):
            sp = filament.match(library, serial, f["type"], f["colour"], hint)
            prices.append((self.prices.get(sp["id"]) or 0) if sp else 0)
            if sp and g:
                if sp["grams_left"] is not None:
                    sp["grams_left"] = max(0, sp["grams_left"] - g)  # a second filament on this spool starts from here
                    self.update_cloud_spool(sp["account"], sp["id"], {"grams_left": sp["grams_left"]})
                used.append({"spool_id": sp["id"], "grams": g})
        job["spools_used"] = used
        job["cost"] = filament.job_cost(grams, prices, job.get("print_seconds") or 0, printer.get("model"),
                                        printer.get("electricity_kwh_price") or 0)
        try:  # the shared copy is only needed until the print is done; retry is for failed jobs
            os.remove(os.path.join(self.share, job["file"]))
        except OSError:
            pass

    def discovery(self):
        """Supervisor discovery message that hands the integration host, ports and secret."""
        return {"service": "druckwerk", "config": {"host": self.hostname_fn(), "bridge_port": self.bridge[1],
                                                       "hub_port": self.port, "secret": self.secret}}

    def auto_register(self, listed=None):
        """Register every printer that the bridge lists (the accounts' bound printers), so an admin needs no setup step."""
        for d in listed if listed is not None else self.bridge_ok("GET", "/api/printers", timeout=20):
            serial = d["serial"]
            if self.registry.get(serial) or self.registry.is_removed(serial):
                continue
            name = d.get("name") or serial
            prefix = self.default_prefix(serial, name)
            self.registry.put(serial, name=name, model=d.get("dev_product_name") or d.get("dev_model_name") or "",
                              account=d.get("account"), prefix=prefix, plate_type="", pause_entities=[],
                              electricity_kwh_price=0)
            log("registered printer %s as %s" % (serial, prefix))
            self.grants.migrate_owners()  # an older "all printers" owner grant applies to the new printer

    def try_auto_register(self, every=60, listed=None):
        # Throttled: the bridge or HA may be down, and each try costs two calls.
        now = self.clock()
        if self._auto_at is not None and now - self._auto_at < every:
            return
        self._auto_at = now
        try:
            self.auto_register(listed)
        except Exception as e:  # noqa: BLE001 - bridge and HA errors must not stop the ticker
            log("auto register: %s" % e)

    def tick_all(self):
        try:
            self.watchdog.tick()
        except Exception as e:  # noqa: BLE001 - HA may be down; the next tick follows
            log("watchdog: %s" % e)
        self.try_auto_register()
        for p in self.registry.all():
            try:
                self.queue.tick(p["serial"])
            except Exception as e:  # noqa: BLE001 - one printer's HA error must not stop the others
                log("tick %s: %s" % (p["serial"], e))
        try:
            self.sync()
        except Exception as e:  # noqa: BLE001
            log("sync: %s" % e)

    def run_ticker(self, stop, every=10):
        self.try_auto_register()  # at startup, before the first wait
        while not stop.wait(every):
            self.tick_all()

    def on_state(self, entity_id, state):
        self.watchdog.on_state(entity_id, state)
        for p in self.registry.all():
            if entity_id == self.entity(p, "stage"):
                self.queue.observe(p["serial"], state)

    # -- request plumbing --
    def need(self, c, need):
        if need == "ha_admin" and not self.access.is_global_admin(c):
            raise Forbidden("rule: only Home Assistant admins can do this")
        if need == "internal" and not c.internal:
            raise Forbidden("rule: only the Home Assistant integration can do this")

    def wrap(self, fn, need="user"):
        def run(h, m, q):
            c = self.caller(h)
            self.need(c, need)
            h.send(200, fn(h, m, q, c))
        return run

    def dispatch(self, h, method, routes):
        u = urlsplit(h.path)
        internal = self.is_internal(h)
        if not internal and self.trusted_ips and h.client_address[0] not in self.trusted_ips:
            for k in IDENTITY_HEADERS:  # only the Supervisor's ingress gateway may vouch for a user
                del h.headers[k]
        for meth, regex, fn in routes:
            m = meth == method and re.fullmatch(regex, u.path)
            if not m:
                continue
            # Mutating requests are answered after sync(), so a client that reads the answer then polls events sees the change.
            out = Capture(h) if method != "GET" else h
            try:
                if not self.is_public(u.path) and not internal and not (h.headers.get("X-Remote-User-Id") or "").strip():
                    raise Unauthorized("missing X-Remote-User-Id: requests need a Home Assistant identity")
                fn(out, m, parse_qs(u.query))
            except Unauthorized as e:
                return h.send(401, {"error": str(e)})
            except Forbidden as e:
                return h.send(403, {"error": str(e)})
            except NotFound as e:
                return h.send(404, {"error": str(e)})
            except QueueError as e:
                return h.send(409, {"error": str(e)})
            except HttpError as e:
                return h.send(e.status, {"error": str(e)})
            except HAUnavailable as e:
                return h.send(503, {"error": str(e), "retry": True})
            except HAError as e:
                return h.send(502, {"error": str(e)})
            except ValueError as e:
                return h.send(400, {"error": str(e)})
            if out is not h:
                try:
                    self.sync()
                except Exception as e:  # noqa: BLE001
                    log("sync: %s" % e)
                if out.out:
                    h.send(*out.out)
            return
        h.send(404, {"error": "not found"})

    def is_public(self, path):
        """The web UI's own files carry no data, so they load without identity (the API behind them does not)."""
        return bool(self.ui_dir) and (path == "/" or re.fullmatch("/" + UI_FILES, path) is not None)

    def get_ui(self, h, m, q):
        name = (m.group(1) if m.re.groups else None) or "index.html"
        try:
            with open(os.path.join(self.ui_dir, name), "rb") as f:
                h.send(200, f.read(), MIME[name.rsplit(".", 1)[1]])
        except OSError:
            raise NotFound("no such file")

    # -- route functions --
    def role_or_none(self, c, serial):
        return self.access.role(c, serial)

    def get_me(self, h, m, q, c):
        roles = [{"serial": p["serial"], "role": self.access.role(c, p["serial"])} for p in self.registry.all()]
        u = self.get_user(c.user_id) or {}
        return {"user_id": c.user_id, "name": c.display or u.get("name", ""), "is_admin": self.access.is_global_admin(c),
                "setup_done": bool(self.registry.all()), "printers": [r for r in roles if r["role"]]}

    def get_setup(self, h, m, q, c):
        health = self.bridge_ok("GET", "/api/health", timeout=10)
        listed = self.bridge_ok("GET", "/api/printers", timeout=20)
        self.try_auto_register(every=0, listed=listed)
        printers = []
        for d in listed:
            serial = d["serial"]
            cfg = self.registry.get(serial)
            printers.append({"serial": serial, "device_name": d.get("name") or serial,
                             "model": d.get("dev_product_name") or d.get("dev_model_name") or "", "account": d.get("account"),
                             "configured": bool(cfg), "suggested_prefix": self.default_prefix(serial, d.get("name") or serial),
                             "config": self.config_view(cfg) if cfg else None})
        return {"accounts": [{"id": a["id"], "email": a["email"], "logged_in": a.get("logged_in", True),
                              "daemon_running": bool((a.get("daemon") or {}).get("running"))} for a in health["accounts"]],
                "printers": printers,
                "plates_inbox": self.plates_inbox.status() if self.plates_inbox else {"enabled": False}}

    @staticmethod
    def config_view(p):
        return {"serial": p["serial"], "name": p["name"], "model": p["model"], "account": p["account"],
                "ha_prefix": p["prefix"], "plate": p["plate_type"], "nozzles": p["nozzles"],
                "pause_entities": p["pause_entities"], "electricity_kwh_price": p.get("electricity_kwh_price") or 0,
                "owner": p.get("owner") or ""}

    def login(self, step):
        def run(h, m, q, c):
            b = h.json()
            return self.bridge_ok("POST", "/api/login/" + step, b, timeout=120)
        return run

    def delete_account(self, h, m, q, c):
        return self.bridge_ok("DELETE", "/api/accounts/" + m.group(1))

    def put_printer(self, h, m, q, c):
        serial, b = m.group(1), h.json()
        price = b.get("electricity_kwh_price", 0)
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0:
            raise ValueError("electricity_kwh_price must be a number >= 0")
        known = next((d for d in self.bridge_ok("GET", "/api/printers", timeout=20) if d["serial"] == serial), None)
        old = self.registry.get(serial)
        if not known and not old:
            raise NotFound("the logged-in accounts have no printer with that serial")
        owner = b.get("owner", old.get("owner") if old else "")  # no key: keep the owner
        if owner and not self.get_user(owner):
            raise ValueError("owner must be an active Home Assistant user id (or empty for none)")
        prefix = str(b.get("ha_prefix") or "").strip() or self.default_prefix(serial, b.get("name") or serial)
        model = (known and (known.get("dev_product_name") or known.get("dev_model_name"))) or old["model"]
        p = self.registry.put(serial, name=b.get("name"), model=model, account=known["account"] if known else old["account"],
                              prefix=prefix, plate_type=b.get("plate") or "", nozzles=b["nozzles"] if "nozzles" in b else old and old["nozzles"],
                              pause_entities=b.get("pause_entities") or [], electricity_kwh_price=price, owner=owner)
        return self.config_view(p)

    def delete_printer(self, h, m, q, c):
        if not self.registry.get(m.group(1)):
            raise NotFound("unknown printer")
        self.registry.remove(m.group(1))
        self.registry.mark_removed(m.group(1))
        return {"removed": m.group(1)}

    def get_integration(self, h, m, q, c):
        try:
            connected = bool(self.ha.command("config_entries/get", domain="druckwerk"))
        except Exception as e:  # noqa: BLE001 - the page must still show the manual values
            log("config entries: %s" % e)
            connected = False
        return {"url": "http://%s:%s" % (self.hostname_fn(), self.port), "secret": self.secret, "connected": connected}

    def get_printers(self, h, m, q, c):
        out = []
        for p in self.registry.all():
            role = self.access.role(c, p["serial"])
            if role:
                out.append(self.printer_view(p, role))
        return out

    def printer_access(self, c, serial, admin=False):
        p = self.registry.get(serial)
        role = self.access.role(c, serial) if p else None
        if not role:
            raise NotFound("unknown printer") if not p else Forbidden("rule: you have no active grant on this printer")
        return p

    def get_camera(self, h, m, q, c):
        serial = m.group(1)
        self.printer_access(c, serial)
        kind = "snapshot" if (q.get("mode") or [""])[0] == "snapshot" else "live"
        query = urlsplit(h.path).query if kind == "live" else "fresh=1" if (q.get("fresh") or [""])[0] == "1" else ""
        self.proxy(h, "/api/camera/%s/%s" % (serial, kind), query)

    def cover_png(self, serial):
        """The plate image of the file Druckwerk sent for the current print (or the file named like the printer's task)."""
        task = (self.printer_status(serial) or {}).get("task_name")
        return self.integration.preview(serial, self.integration.wanted(serial, self.sender.result(serial)["last_path"], task))[1]

    def task_cover(self, serial, task):
        """The cloud's cover of the newest task with the printer's task name; None without one."""
        wanted = history.names(task)
        try:
            tasks = self.cloud_tasks(serial)
        except HttpError:
            return None
        hits = [t for t in tasks if t.get("cover_url") and history.names(t.get("title"), t.get("design_title")) & wanted]
        return max(hits, key=lambda t: t.get("started_at") or 0)["cover_url"] if hits and wanted else None

    def get_cover(self, h, m, q, c):
        serial = self.printer_access(c, m.group(1))["serial"]
        png = self.cover_png(serial)
        if png:
            return h.send(200, png, "image/png")
        url = self.task_cover(serial, (self.printer_status(serial) or {}).get("task_name"))
        if not url:
            raise NotFound("no cover image for this print")
        h.send(200, *fetch_image(url))

    def cloud_tasks(self, serial):
        return self._cached("tasks:" + serial, TASKS_TTL, lambda: self.bridge_ok("GET", "/api/tasks?serial=%s&limit=100" % serial, timeout=30))

    def get_history(self, h, m, q, c):
        def one(name):
            return (q.get(name) or [""])[0]
        limit = num(one("limit") or 100, int)
        if limit is None or not 1 <= limit <= 500:
            raise ValueError("limit must be a number from 1 to 500")
        return self.history_items(c, one("user"), one("printer"), limit)

    def history_items(self, c, who, printer, limit):
        """Cloud tasks with their Druckwerk job merged in, plus finished jobs that no task explains."""
        shown = [(p, self.access.role(c, p["serial"])) for p in self.registry.all() if printer in ("", p["serial"])]
        tasks, unavailable = [], []
        for p, role in shown:
            if role:
                try:
                    tasks += self.cloud_tasks(p["serial"])
                except HttpError as e:
                    log("history %s: %s" % (p["serial"], e))
                    unavailable.append(p["serial"])
        owned = history.attribute(tasks, history.jobs_of(self.queue))
        by_serial = {p["serial"]: (p, role) for p, role in shown}
        merged = {j["id"] for j in owned.values()}
        # Only jobs the caller may see; the same owner rule as for tasks applies below.
        loose = [j for j in self.access.bind(self.queue, c).snapshot()["history"]
                 if j["id"] not in merged and j["state"] in history.FINAL and j.get("kind") != "dry"
                 and printer in ("", j.get("printer") or (j["target"] if j["target"] != "any" else ""))]
        out = []
        for t in tasks:
            p, role = by_serial[t["serial"]]
            out.append((history.item(t, owned.get(t["id"]), p), role))
        for j in loose:
            serials = self.access.serials_of(j)
            p = self.registry.get(serials[0]) if len(serials) == 1 else None
            out.append((history.job_item(j, p), self.access.best(c, serials)))
        owners = {}
        res = []
        for it, role in out:
            if not it["user_id"]:  # nobody started it here: the printer's owner is the closest answer
                it["printer_owner"] = owners[it["serial"]] if it["serial"] in owners else owners.setdefault(it["serial"], self.printer_owner(it["serial"]))
            if role == "admin":  # the user filter is for admins; everyone else sees their own prints only
                if who and (it["user_id"] or "unknown") != who:
                    continue
            elif it["user_id"] != c.user_id:
                continue
            res.append(it)
        res.sort(key=lambda i: i["when"] or 0, reverse=True)
        return {"items": res[:limit], "unavailable": unavailable}

    def printer_owner(self, serial):
        """Display name of the printer's owner, None without one."""
        uid = (self.registry.get(serial) or {}).get("owner")
        return uid and ((self.get_user(uid) or {}).get("name") or uid) or None

    # -- settings of a history item, and a new print job with the same settings --
    def history_item(self, c, hid):
        """One history item as the caller may see it."""
        for it in self.history_items(c, "", "", 500)["items"]:
            if str(it["id"]) == hid:
                return it
        raise NotFound("unknown history item")

    def kept_slice(self, it):
        """The slicer's finished job for a history item while the slicer still has it, else None."""
        sj = self.s.jobs.get(((it.get("job") or {}).get("slice_job")) or "")
        return sj if sj and sj.get("state") == "done" and os.path.isfile(sj.get("result") or "") else None

    def saved_settings_path(self, job_id):
        return os.path.join(self.data_dir, "settings", "%s.json" % job_id)

    def save_settings(self, job_id, path):
        """Keep the parsed settings of a queued file: the slicer and the share forget the file, this is small."""
        parsed = slice_settings.read(path)
        if parsed:
            os.makedirs(os.path.join(self.data_dir, "settings"), exist_ok=True)
            with open(self.saved_settings_path(job_id), "w") as f:
                json.dump(parsed, f)

    def cloud_file(self, it):
        """Path of the item's sliced 3MF from the Bambu cloud (fetched once, kept in data/cloud); ValueError says why not."""
        if it["source"] == "job" or not re.fullmatch(r"\d{1,19}", str(it["id"])):
            raise ValueError("This print has no Bambu cloud task.")
        dest = os.path.join(self.data_dir, "cloud", "%s.gcode.3mf" % it["id"])
        if os.path.isfile(dest):
            return dest
        miss = self.cloud_miss.get(it["id"])
        if miss and self.clock() - miss[0] < MISS_TTL:
            raise ValueError(miss[1])
        try:
            if not (it.get("model_id") and it.get("profile_id")):
                raise ValueError("The Bambu cloud record of this print has no file reference (modelId, profileId).")
            try:
                got = self.bridge_ok("GET", "/api/tasks/file?serial=%s&model_id=%s&profile_id=%s" % (
                    quote(it["serial"]), quote(str(it["model_id"])), quote(str(it["profile_id"]))), timeout=30)
                fetch_file(got["url"], dest)
            except (HttpError, KeyError, TypeError) as e:
                raise ValueError("The Bambu cloud did not give the file (%s)." % e)
            if not slice_settings.read(dest):
                os.remove(dest)
                raise ValueError("The Bambu cloud file has no slicing settings.")
        except ValueError as e:
            self.cloud_miss[it["id"]] = (self.clock(), str(e))
            raise
        return dest

    def history_files(self, it):
        """What Druckwerk still holds for a print: {'model': (path, name), 'sliced': (path, name)}, either may be missing."""
        job, out = it.get("job") or {}, {}
        sj = self.s.jobs.get(job.get("slice_job") or "")
        if sj and os.path.isfile(sj.get("model") or ""):
            out["model"] = (sj["model"], os.path.basename(sj.get("filename") or sj["model"]))
        path = (self.kept_slice(it) or {}).get("result")
        if not path and job.get("file") and os.path.isfile(os.path.join(self.share, os.path.basename(job["file"]))):
            path = os.path.join(self.share, os.path.basename(job["file"]))
        if not path:
            try:
                path = self.cloud_file(it)
            except ValueError:
                pass
        if path:
            out["sliced"] = (path, re.sub(r"[^A-Za-z0-9._ -]+", "_", str(it.get("title") or job.get("name") or "print")).strip("_")[:60] + ".gcode.3mf")
        return out

    def get_history_file(self, h, m, q, c):
        """The source model, else the sliced 3MF; ?kind=sliced asks for the 3MF. Only for a print the caller may see."""
        it = self.history_item(c, unquote(m.group(1)))
        files = self.history_files(it)
        want = (q.get("kind") or [""])[0]
        path, _name = files.get("sliced" if want == "sliced" else "model") or files.get("sliced") or (None, None)
        if not path:
            raise NotFound("no stored file for this print")
        with open(path, "rb") as f:
            h.send(200, f.read(), "application/octet-stream")

    def get_history_gcode(self, h, m, q, c):
        """The first plate's G-code out of the sliced file, for the 3D preview."""
        it = self.history_item(c, unquote(m.group(1)))
        path = (self.history_files(it).get("sliced") or ("",))[0]
        try:
            with zipfile.ZipFile(path) as z:
                names = sorted(n for n in z.namelist() if SLICED.fullmatch(n))
                if names:
                    return h.send(200, z.read(names[0]), "text/plain; charset=utf-8")
        except (OSError, zipfile.BadZipFile):
            pass
        raise NotFound("no G-code for this print")

    def get_history_settings(self, h, m, q, c):
        it = self.history_item(c, unquote(m.group(1)))
        job = it.get("job") or {}
        keep = ("title", "design_title", "status_code", "plate", "seconds", "grams", "started_at", "ended_at", "model_id", "profile_id", "design_id")
        out = {"available": False, "source": None, "reason": "", "can_reprint": False,
               "task": {k: it[k] for k in keep if it.get(k) not in (None, "")}, "groups": [], "all": [], "plate": {}, "slicer": None}
        path, source, sj = None, None, self.kept_slice(it)
        if sj:
            path, source = sj["result"], "slice"
        elif job and os.path.isfile(self.saved_settings_path(job["id"])):
            source = "saved"
        if not source:
            try:
                path, source = self.cloud_file(it), "cloud"
            except ValueError as e:
                out["reason"] = str(e)
        if source == "saved":
            with open(self.saved_settings_path(job["id"])) as f:
                parsed = json.load(f)
        else:
            parsed = path and slice_settings.read(path)
        if parsed:
            out.update(parsed, available=True, source=source, reason="")
            if source == "slice" and job:
                self.save_settings(job["id"], path)
            out["can_reprint"] = bool(path) and self.sliced_file(path)
        files = self.history_files(it)
        kind = "model" if "model" in files else "sliced" if "sliced" in files else None
        out["download"] = kind and {"kind": kind, "name": files[kind][1]}
        out["preview"] = "sliced" in files and self.sliced_file(files["sliced"][0])
        return out

    @staticmethod
    def sliced_file(path):
        try:
            with zipfile.ZipFile(path) as z:
                return any(SLICED.fullmatch(n) for n in z.namelist())
        except (OSError, zipfile.BadZipFile):
            return False

    def post_history_queue(self, h, m, q, c):
        """The same file as a new queue job: the print's target and options unless the body names others."""
        it = self.history_item(c, unquote(m.group(1)))
        job, b = it.get("job") or {}, h.json()
        any_ = (b.get("target") or job.get("target")) == "any"
        body = {"target": b.get("target") or job.get("printer") or job.get("target") or it["serial"],
                "options": b.get("options") or job.get("options") or None, "name": b.get("name") or job.get("name") or it.get("title"),
                **({"model": b.get("model") or job.get("model")} if any_ else {})}
        sj = self.kept_slice(it)
        if sj:
            self.own(c, sj["id"])
            return self.queue_slice(c, sj, body)
        try:
            path = self.cloud_file(it)
        except ValueError as e:
            raise QueueError("No stored file for this print: %s Slice the model again." % e)
        if not self.sliced_file(path):
            raise QueueError("The cloud file is not sliced. Slice the model again.")
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", str(it.get("title") or "")).strip("_")[:40] or "print"
        return self.queue_real(c, path, body, stem + ".gcode.3mf")

    def get_spool_prints(self, h, m, q, c):
        """The settled prints that used one spool, newest first, as history items."""
        sid = m.group(1)
        items = []
        for it in self.history_items(c, "", "", 500)["items"]:
            used = [u for u in (it.get("job") or {}).get("spools_used") or [] if str(u.get("spool_id")) == sid]
            if used:
                items.append(dict(it, spool_grams=sum(u.get("grams") or 0 for u in used)))
        return {"items": items}

    # -- slices and jobs --
    def own(self, c, job_id):
        job = self.s.jobs.get(job_id)
        if not job:
            raise NotFound("unknown job")
        if not (self.access.is_global_admin(c) or job.get("owner_user_id") == c.user_id):
            raise Forbidden("rule: slices belong to the person who uploaded them")
        return job

    def slicer_job(self, fn, check=None):
        def run(h, m, q):
            c = self.caller(h)
            job = self.own(c, m.group(1))
            if check:
                check(h, c, job)
            return fn(h, m, q)
        return run

    def get_slice(self, h, m, q):
        """The job answer plus a cost estimate once the browser's G-code is stored."""
        job = self.own(self.caller(h), m.group(1))
        cap = Capture(h)
        self.s.get_job(cap, m, q)
        status, body, ctype = cap.out
        if status == 200 and body.get("state") == "done" and job.get("result"):
            try:
                body["cost_estimate"] = self.estimate_cost(job)
            except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                body["cost_estimate"] = None
        h.send(status, body, ctype)

    def estimate_cost(self, job):
        """Same formula as settle(): spools matched on the printer, grams and print time from the slice."""
        serial = job["printer"]["serial"]
        printer = self.registry.get(serial) or {}
        use = filament.slice_usage(job["result"])
        tray = (job.get("settings") or {}).get("tray")
        hint = (int(tray["ams"]), int(tray["id"])) if tray and tray.get("ams") is not None and len(use["filaments"]) == 1 else None
        prices, library = [], self.printer_spools(serial)
        for f in use["filaments"]:
            sp = filament.match(library, serial, f["type"], f["colour"], hint)
            prices.append((self.prices.get(sp["id"]) or 0) if sp else 0)
        return filament.job_cost([f["grams"] for f in use["filaments"]], prices, use["seconds"], printer.get("model"),
                                 printer.get("electricity_kwh_price") or 0)

    def check_gcode(self, h, c, job):
        """Browser-sliced G-code needs an active grant on the printer the query names."""
        serial = (parse_qs(urlsplit(h.path).query).get("serial") or [""])[0]
        if self.access.role(c, serial) is None:
            raise Forbidden("rule: slicing needs an active grant on the printer")

    def get_engine_filament(self, h, m, q):
        c = self.caller(h)
        if self.access.role(c, (q.get("serial") or [""])[0]) is None:
            raise Forbidden("rule: slicing needs an active grant on the printer")
        self.s.get_filament(h, m, q)

    def check_print(self, h, c, job):
        serial = (job.get("printer") or {}).get("serial")
        if self.access.role(c, serial) != "admin":
            raise Forbidden("rule: only an admin of the printer can print directly; use the queue")

    def post_job(self, h, m, q):
        c = self.caller(h)
        cap = Capture(h)
        self.s.post_job(cap, m, q)
        status, body, ctype = cap.out
        if status == 200:
            job = self.s.jobs[body["id"]]
            job["owner_user_id"] = c.user_id
            self.s.save_job(job)
            body = dict(self.s.public(job), **({"received": body["received"]} if "received" in body else {}))
        h.send(status, body, ctype)

    def queue_job(self, h, m, q, c):
        return self.queue_slice(c, self.own(c, m.group(1)), h.json())

    def queue_slice(self, c, job, b):
        if job.get("state") != "done":
            raise QueueError("slice the model first")
        target = b.get("target")
        if not target:
            raise ValueError("target is required: a serial or any")
        model = b.get("model") or job["printer"]["model"]
        use = filament.slice_usage(job["result"])
        name = self.s.print_name(job)
        name = name[:-len(".gcode.3mf")].replace("..", "_") + "_" + secrets.token_hex(3) + ".gcode.3mf"
        added = self.access.bind(self.queue, c).add(
            name, job["filename"], target, model,
            [{"type": f["type"], "colour": f["colour"]} for f in use["filaments"]], b.get("options"),
            name=b.get("name") or job["filename"].rsplit(".", 1)[0])
        os.makedirs(self.share, exist_ok=True)
        with open(job["result"], "rb") as src:
            # Written after the job passed the access check, so a refused request leaves nothing in the share.
            with open(os.path.join(self.share, name), "wb") as dst:
                dst.write(src.read())
        png = None
        with zipfile.ZipFile(job["result"]) as z:
            if "Metadata/plate_1.png" in z.namelist():
                with open(os.path.join(self.data_dir, "plates", added["id"] + ".png"), "wb") as f:
                    f.write(z.read("Metadata/plate_1.png"))
                png = "api/queue/%s/plate.png" % added["id"]
        self.save_settings(added["id"], job["result"])
        tray = (job.get("settings") or {}).get("tray")
        with self.queue.lock:
            added.update(nozzle=job.get("nozzle") or file_nozzle(job["result"]), grams=use["grams"], filament_grams=[f["grams"] for f in use["filaments"]],
                         print_seconds=use["seconds"], plate_png=png,
                         slice_tray=[int(tray["ams"]), int(tray["id"])] if tray and tray.get("ams") is not None else None,
                         slice_spool={k: tray.get(k) for k in ("spool_id", "brand", "type", "name", "colour")}
                         if tray and tray.get("ams") is None else None,
                         slice_job=job["id"], slice=self.slice_view(job, use))
            self.queue._save()
        return added

    @staticmethod
    def slice_view(job, use):
        """What the slice was made with, kept on the queue job: the slicer forgets its jobs after ten newer ones."""
        s = job.get("settings") or {}
        return {"quality": job.get("quality"), "process": s.get("process"), "filament": s.get("filament"), "bed": s.get("bed"),
                "tray": s.get("tray"), "seconds": use["seconds"], "grams": use["grams"], "filaments": use["filaments"],
                "model": job.get("filename")}

    def resolve_media(self, path):
        """The real path of a sliced .gcode.3mf under /media or /share, or ValueError. Symlinks are resolved first."""
        real = os.path.realpath(path)
        roots = [os.path.realpath(r) for r in self.media_roots]
        if not any(real == r or real.startswith(r + os.sep) for r in roots):
            raise ValueError("the file must be under /media or /share")
        if not real.endswith(".gcode.3mf") or not os.path.isfile(real):
            raise ValueError("no such .gcode.3mf file")
        return self.check_sliced(real)

    @staticmethod
    def check_sliced(real):
        try:
            with zipfile.ZipFile(real) as z:
                if not any(SLICED.fullmatch(n) for n in z.namelist()):
                    raise ValueError("that 3MF is not sliced: it has no Metadata/plate_N.gcode")
                z.read("Metadata/slice_info.config")
        except (zipfile.BadZipFile, KeyError):
            raise ValueError("that file is not a sliced .gcode.3mf")
        return real

    def post_queue(self, h, m, q, c):
        """POST /api/queue: the queue's own add, plus a file named by absolute path, which is copied into the share."""
        b = h.json()
        f = b.get("file")
        api = self.access.bind(self.queue, c)
        if b.get("kind") == "dry" or not (isinstance(f, str) and f.startswith("/")):
            return self.queue_routes_add(api, b)
        return self.queue_sliced(api, self.resolve_media(f), b)

    def queue_real(self, c, real, b, shown):
        return self.queue_sliced(self.access.bind(self.queue, c), real, b, shown)

    def queue_sliced(self, api, real, b, shown=None):
        """Add a sliced .gcode.3mf at `real` to the queue (copied into the share) and return the job; b is the POST body."""
        shown = shown or os.path.basename(real)
        use = filament.slice_usage(real)
        base = shown[:-len(".gcode.3mf")].replace("..", "_")
        name = base + "_" + secrets.token_hex(3) + ".gcode.3mf"
        fils = b.get("filaments") or [{"type": x["type"], "colour": x["colour"]} for x in use["filaments"]]
        added = api.add(name, shown, b.get("target", "any"), b.get("model", ""), fils, b.get("options"),
                        name=b.get("name") or base)
        self.stamp_nozzle(added, real)
        os.makedirs(self.share, exist_ok=True)
        with open(real, "rb") as src, open(os.path.join(self.share, name), "wb") as dst:  # after the access check
            dst.write(src.read())
        png = None
        with zipfile.ZipFile(real) as z:
            if "Metadata/plate_1.png" in z.namelist():
                with open(os.path.join(self.data_dir, "plates", added["id"] + ".png"), "wb") as out:
                    out.write(z.read("Metadata/plate_1.png"))
                png = "api/queue/%s/plate.png" % added["id"]
        self.save_settings(added["id"], real)
        with self.queue.lock:
            added.update(grams=use["grams"], filament_grams=[x["grams"] for x in use["filaments"]],
                         print_seconds=use["seconds"], plate_png=png)
            self.queue._save()
        return added

    def queue_routes_add(self, api, b):
        if b.get("kind") == "dry":
            return api.add_dry(b.get("target"), b.get("ams_id"), b.get("temp"), b.get("hours"), b.get("filament", ""),
                               name=b.get("name"))
        job = api.add(b.get("file"), b.get("source", ""), b.get("target", "any"), b.get("model", ""),
                      b.get("filaments"), b.get("options"), name=b.get("name"))
        self.stamp_nozzle(job, os.path.join(self.share, job["file"]))
        return job

    def stamp_nozzle(self, job, path, diameter=None):
        """Keep the nozzle a file was sliced for on its job (None when the file does not say)."""
        with self.queue.lock:
            job["nozzle"] = diameter or file_nozzle(path)
            self.queue._save()

    def post_drying(self, h, m, q, c):
        """Dry now: a queue job that starts at once, so the busy checks and the queue view are the same as for a queued one."""
        serial = m.group(1)
        self.printer_access(c, serial)
        if self.access.role(c, serial) != "admin":
            raise Forbidden("rule: only an admin of the printer can dry an AMS")
        b = h.json()
        api = self.access.bind(self.queue, c)
        job = api.add_dry(serial, b.get("ams_id"), b.get("temp"), b.get("hours"), b.get("filament", ""), name=b.get("name"))
        try:
            return api.start(serial, job_id=job["id"], confirmed=b.get("confirmed"))
        except BaseException:
            with self.queue.lock:  # a refused start leaves no job behind
                if job in self.queue.jobs:
                    self.queue.jobs.remove(job)
                self.queue._save()
            raise

    def post_printer_light(self, h, m, q, c):
        serial = m.group(1)
        self.printer_access(c, serial)
        if self.access.role(c, serial) != "admin":
            raise Forbidden("rule: only an admin of the printer can switch its light")
        on = (h.json() or {}).get("on")
        if not isinstance(on, bool):
            raise ValueError('Body must be {"on": true|false}.')
        self.bridge_ok("POST", "/api/light/" + serial, {"on": on}, timeout=60)
        return {"on": on}

    def post_drying_stop(self, h, m, q, c):
        serial = m.group(1)
        self.printer_access(c, serial)
        if self.access.role(c, serial) != "admin":
            raise Forbidden("rule: only an admin of the printer can stop a drying")
        ams_id = (h.json() or {}).get("ams_id")
        if isinstance(ams_id, bool) or not isinstance(ams_id, int):
            raise ValueError("ams_id is required")
        job = next((j for j in self.queue.jobs if j.get("kind") == "dry" and j["state"] == "drying"
                    and j.get("printer") == serial and j["dry"]["ams_id"] == ams_id), None)
        if job:
            return self.queue.stop_dry(job["id"])
        self.bridge_ok("POST", "/api/ams/%s/drying/stop" % serial, {"ams_id": ams_id})
        return {"stopped": ams_id}

    def get_plate(self, h, m, q, c):
        jid = m.group(1)
        snap = self.access.bind(self.queue, c).snapshot()
        if not any(j["id"] == jid for j in snap["jobs"] + snap["history"]):
            raise NotFound("no such job")
        try:
            with open(os.path.join(self.data_dir, "plates", jid + ".png"), "rb") as f:
                h.send(200, f.read(), "image/png")
        except OSError:
            raise NotFound("no plate image")

    # -- spools (the Bambu Cloud filament library, read and written through the bridge) --
    def library(self, account, fetch=True):
        """An account's spools, reused for SPOOLS_TTL. fetch=False only reads what is cached, however old."""
        key = "lib:%s" % account
        with self._cache_lock:
            at, val = self._cache.get(key, (0, None))
        if val is not None and (not fetch or self.clock() - at < SPOOLS_TTL):
            return val
        if not fetch or not account:
            return []
        try:
            val = self.bridge_ok("GET", "/api/filament?account=" + quote(account), timeout=30)
        except HttpError as e:  # no stale copy: the person must see that the cloud did not answer
            raise HttpError(e.status, "Bambu Cloud filament library: %s" % e)
        with self._cache_lock:
            self._cache[key] = (self.clock(), val)
        self.loads.settle(val)
        return val

    def printer_spools(self, serial):
        """Copies of the spools of the printer's account; empty when the cloud does not answer, so a print still settles."""
        account = (self.registry.get(serial) or {}).get("account")
        try:
            return [dict(s) for s in self.library(account)] if account else []
        except HttpError as e:
            log("filament library: %s" % e)
            return []

    def update_cloud_spool(self, account, spool_id, fields):
        try:
            self.bridge_ok("PUT", "/api/filament/%s?account=%s" % (spool_id, quote(account)), fields, timeout=30)
        except HttpError as e:
            log("spool %s not updated in the cloud: %s" % (spool_id, e))
        self._cache.pop("lib:%s" % account, None)

    def library_accounts(self, c):
        """Cloud accounts whose library the caller sees: all for HA admins, else those that own a printer the caller sees."""
        if self.access.is_global_admin(c):
            found = self._cached("accounts", 60, lambda: {p["account"] for p in self.bridge_ok("GET", "/api/printers", timeout=20) if p.get("account")})
        else:
            found = {p.get("account") for p in self.registry.all() if self.access.role(c, p["serial"])} - {None}
        if not found:
            raise HttpError(409, "No Bambu Cloud account is linked for your printers; link one in the bridge setup.")
        return sorted(found)

    def visible_spools(self, c):
        out = []
        for acc in self.library_accounts(c):
            # Spools loaded in a printer the caller cannot see stay hidden from non-admins.
            out += [dict(s, price_per_kg=self.prices.get(s["id"])) for s in self.library(acc)
                    if self.access.is_global_admin(c) or not s["printer"] or self.access.role(c, s["printer"])]
        return out

    def spool_admin(self, c, account):
        """HA admins and the admin or owner of one of the account's printers manage its spools."""
        if not (self.access.is_global_admin(c) or any(p.get("account") == account and self.access.role(c, p["serial"]) == "admin"
                                                      for p in self.registry.all())):
            raise Forbidden("rule: only an admin of the spool's printer (or a Home Assistant admin) manages spools")

    def find_spool(self, c, spool_id):
        spool = next((s for s in self.visible_spools(c) if s["id"] == spool_id), None)
        if not spool:
            raise NotFound("no such spool")
        self.spool_admin(c, spool["account"])
        return spool

    def split_price(self, b):
        """(cloud fields, price) of a request body; the price stays in the hub."""
        b = dict(b)
        price = b.pop("price_per_kg", "keep")
        if price not in (None, "keep") and (isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0):
            raise ValueError("price_per_kg must be a number >= 0")
        return b, price

    def get_spools(self, h, m, q, c):
        return self.visible_spools(c)

    def post_spool(self, h, m, q, c):
        fields, price = self.split_price(h.json())
        account = fields.pop("account", None)
        if not account:
            accounts = self.library_accounts(c)
            if len(accounts) != 1:
                raise ValueError("account is required: " + ", ".join(accounts))
            account = accounts[0]
        if account not in self.library_accounts(c):
            raise Forbidden("rule: this Bambu Cloud account is not one of yours")
        self.spool_admin(c, account)
        made = self.bridge_ok("POST", "/api/filament?account=" + quote(account), fields)
        self._cache.pop("lib:%s" % account, None)
        if made.get("id") is not None and price not in (None, "keep"):
            self.prices.set(made["id"], price)
        return dict(made, price_per_kg=self.prices.get(made.get("id")))

    def put_spool(self, h, m, q, c):
        old = self.find_spool(c, m.group(1))
        fields, price = self.split_price(h.json())
        made = {}
        if fields:
            made = self.bridge_ok("PUT", "/api/filament/%s?account=%s" % (old["id"], quote(old["account"])), fields)
            self._cache.pop("lib:%s" % old["account"], None)
        if price != "keep":
            self.prices.set(old["id"], price)
        return {**old, **fields, **made, "price_per_kg": self.prices.get(old["id"])}

    def delete_spool(self, h, m, q, c):
        old = self.find_spool(c, m.group(1))
        self.bridge_ok("DELETE", "/api/filament/%s?account=%s" % (old["id"], quote(old["account"])))
        self._cache.pop("lib:%s" % old["account"], None)
        self.prices.set(old["id"], None)
        return {"removed": old["id"]}

    # -- load requests: a user asks the printer's owner to put a library spool into the AMS --
    def load_request_visible(self, c, r):
        return self.access.role(c, r["printer"]) is not None

    def get_load_requests(self, h, m, q, c):
        self.settle_requests(fresh=True)
        return [r for r in list(self.loads.items) if self.load_request_visible(c, r)]

    def settle_requests(self, fresh=False):
        """Close requests whose job is gone or whose change happened. fresh=False reads only cached cloud data."""
        with self.queue.lock:  # taken before the stores' locks: Queue.start holds it while it asks for requests
            active = {j["id"] for j in self.queue.jobs if j["state"] in WAITING}
        self.loads.drop_jobs(active)
        self.nozzle_requests.drop_jobs(active)
        try:
            states = self.ha.states()
            prefix = {p["serial"]: p["prefix"] for p in self.registry.all()}
            self.nozzle_requests.settle(lambda serial: nozzles.installed(states, prefix.get(serial, "")))
        except HAError as e:  # the lists need no Home Assistant
            log("nozzle requests: %s" % e)
        for serial in {r["printer"] for r in list(self.loads.items)}:
            account = (self.registry.get(serial) or {}).get("account")
            try:
                if account:
                    self.loads.settle(self.library(account, fetch=fresh))
            except HttpError as e:  # the list itself needs no cloud
                log("load requests: %s" % e)

    def job_requests(self, job_id, fresh=False):
        """The open requests blocking a job, as the queue shows them."""
        if not any(r.get("job_id") == job_id for r in self.loads.items + self.nozzle_requests.items):
            return []
        self.settle_requests(fresh)
        out = [dict(kind="load", wait="%s to be loaded" % r["spool_label"], **{k: r.get(k) for k in ("id", "printer", "user_id", "user_name", "note", "created_at")})
               for r in self.loads.items if r.get("job_id") == job_id]
        out += [dict(kind="nozzle", wait="%s mm %snozzle" % (r["diameter"], r["type"].replace("_", " ") + " " if r["type"] else ""),
                     **{k: r.get(k) for k in ("id", "printer", "user_id", "user_name", "note", "created_at")})
                for r in self.nozzle_requests.items if r.get("job_id") == job_id]
        return out

    def request_job(self, c, b, serial):
        """The waiting print job a request is for; only its owner or an admin of the printer may ask."""
        job_id = b.get("job_id")
        if not (isinstance(job_id, str) and re.fullmatch("[0-9a-f]{12}", job_id)):
            raise ValueError("job_id is required: ask from a queue entry")
        with self.queue.lock:
            job = next((j for j in self.queue.jobs if j["id"] == job_id and j["state"] in WAITING and j.get("kind") != "dry"), None)
        if not job:
            raise NotFound("no such waiting print job")
        if serial not in self.access.serials_of(job):
            raise ValueError("that job does not print on this printer")
        if self.access.role(c, serial) != "admin" and job.get("owner_user_id") != c.user_id:
            raise Forbidden("rule: only the job's owner or an admin of the printer can ask for a change for it")
        return job

    def post_load_request(self, h, m, q, c):
        b = h.json()
        serial = str(b.get("printer") or "")
        if not serial:
            raise ValueError("printer is required")
        note = b.get("note") or ""
        if not isinstance(note, str) or len(note) > 200:
            raise ValueError("note must be text of at most 200 characters")
        p = self.printer_access(c, serial)
        job = self.request_job(c, b, serial)
        spool = next((s for s in self.visible_spools(c) if s["id"] == m.group(1)), None)
        if not spool:
            raise NotFound("no such spool")
        if not self.ams(serial):
            raise ValueError("this printer has no AMS")
        if spool.get("printer") == serial and spool.get("in_printer"):
            raise HttpError(409, "this spool is already in that printer")
        label = " ".join(x for x in (spool["brand"], spool["type"], spool["name"]) if x)
        # Another job already asked for this very spool: the owner was told, so no second message.
        told = any(r["spool_id"] == spool["id"] and r["printer"] == serial for r in self.loads.items)
        req, created = self.loads.add({
            "id": secrets.token_hex(6), "spool_id": spool["id"], "spool_label": label, "colour": spool["colour"],
            "printer": serial, "job_id": job["id"], "job_name": job["name"], "user_id": c.user_id,
            "user_name": c.display or c.name, "note": note.strip(), "created_at": int(self.clock())})
        if created and not told:
            self.emit("load_requested", serial, None, request=dict(req, printer_name=p["name"]))
        return req

    def delete_load_request(self, h, m, q, c):
        r = next((x for x in self.loads.items if x["id"] == m.group(1)), None)
        if not r or not self.load_request_visible(c, r):
            raise NotFound("no such load request")
        if r["user_id"] != c.user_id and self.access.role(c, r["printer"]) != "admin":
            raise Forbidden("rule: only the requester or an admin of the printer can cancel this request")
        self.loads.remove(r["id"])
        return {"removed": r["id"]}

    # -- nozzle requests: a user asks the printer's owner to change the nozzle --
    def get_nozzle_requests(self, h, m, q, c):
        self.settle_requests()
        return [r for r in list(self.nozzle_requests.items) if self.load_request_visible(c, r)]

    def post_nozzle_request(self, h, m, q, c):
        serial, b = m.group(1), h.json()
        diameter, type_, note = str(b.get("diameter") or ""), b.get("type") or "", b.get("note") or ""
        check_nozzles([{"diameter": diameter, "type": type_}])
        if not isinstance(note, str) or len(note) > 200:
            raise ValueError("note must be text of at most 200 characters")
        p = self.printer_access(c, serial)
        job = self.request_job(c, b, serial)
        if nozzles.matches(self.installed_nozzle(p), diameter, type_):
            raise HttpError(409, "this nozzle is already in that printer")
        same = (serial, diameter, type_)  # another job already asked for this nozzle: the owner was told
        told = any((r["printer"], r["diameter"], r["type"]) == same for r in self.nozzle_requests.items)
        req, created = self.nozzle_requests.add({
            "id": secrets.token_hex(6), "printer": serial, "diameter": diameter, "type": type_, "note": note.strip(),
            "job_id": job["id"], "job_name": job["name"], "user_id": c.user_id, "user_name": c.display or c.name,
            "created_at": int(self.clock())})
        if created and not told:
            self.emit("nozzle_requested", serial, None, request=dict(req, printer_name=p["name"]))
        return req

    def delete_nozzle_request(self, h, m, q, c):
        r = next((x for x in self.nozzle_requests.items if x["id"] == m.group(1)), None)
        if not r or not self.load_request_visible(c, r):
            raise NotFound("no such nozzle request")
        if r["user_id"] != c.user_id and self.access.role(c, r["printer"]) != "admin":
            raise Forbidden("rule: only the requester or an admin of the printer can cancel this request")
        self.nozzle_requests.remove(r["id"])
        return {"removed": r["id"]}

    # -- HA lookups, events --
    def get_ha_users(self, h, m, q, c):
        return self.ha_users()

    def get_ha_entities(self, h, m, q, c):
        domains = set(((q.get("domains") or [DOMAIN_DEFAULTS])[0]).split(","))
        out = [{"entity_id": eid, "name": s["attributes"].get("friendly_name") or eid, "state": s["state"]}
               for eid, s in self.ha.states().items() if eid.split(".", 1)[0] in domains]
        return sorted(out, key=lambda e: e["entity_id"])

    def grant_view(self, g):
        u = self.get_user(g["user_id"]) or {}
        return dict(g, user_name=u.get("name") or g["user_id"], active=self.grants.is_active(g))

    def get_grants(self, h, m, q, c):
        return [self.grant_view(g) for g in self.access.bind(self.queue, c).grants_list()]

    def post_grants(self, h, m, q, c):
        from queue_routes import GRANT_FIELDS
        b = h.json()
        return self.grant_view(self.access.bind(self.queue, c).grants_add(**{k: b[k] for k in GRANT_FIELDS if k in b}))

    def create_switch(self, h, m, q, c):
        """An input_boolean for the grant condition, named after the grant's user and printer (needs HA admin, like grants)."""
        b = h.json()
        user = self.get_user(b.get("user_id") or "")
        if not user:
            raise ValueError("user_id must be an active Home Assistant user")
        printer = self.registry.get(b.get("printer") or "")
        if b.get("printer") != "*" and not printer:
            raise ValueError("printer must be a known serial or *")
        name = "Druckwerk %s %s" % (user["name"], printer["name"] if printer else "All printers")
        made = self.ha.command("input_boolean/create", name=name, icon="mdi:printer-3d")
        return {"entity_id": "input_boolean." + made["id"], "name": name, "state": "off"}  # not in the states cache yet

    def post_feedback(self, h, m, q, c):
        """The feedback button: one item of the to-do list todo.feedback, which an HA automation turns into a Claude session."""
        length = content_length(h.headers)
        err = "expected {\"item\": ..., \"description\": ...} of at most %d bytes" % FEEDBACK_MAX
        if not 0 < length <= FEEDBACK_MAX:
            if length <= 4 * FEEDBACK_MAX:  # read the rest so the client gets the 400, not a reset connection
                h.rfile.read(length)
            raise ValueError(err)
        try:
            req = json.loads(h.rfile.read(length))
            item, description = req["item"], req["description"]
            if not isinstance(item, str) or not 0 < len(item) <= 300 or not isinstance(description, str) or not description:
                raise TypeError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError(err)
        self.ha.call_service("todo", "add_item", {"entity_id": "todo.feedback", "item": item, "description": description})
        return {"ok": True}

    def get_events(self, h, m, q, c):
        self.sync()
        rev = actions.manifest_revision()
        if "since" not in q:
            return {"seq": self.seq, "manifest_revision": rev, "events": []}  # a first poll starts from now, no backlog
        since = num(q["since"][0], int)
        if since is None:
            raise ValueError("since must be an integer")
        return {"seq": self.seq, "manifest_revision": rev, "events": [e for e in list(self.events) if e["seq"] > since]}

    def internal_caller(self, h):
        """The integration's routes: the secret is required, the act-as user's rules apply as for the web UI."""
        if not self.is_internal(h):
            raise Forbidden("rule: only the Home Assistant integration can do this")
        return self.caller(h)

    def get_manifest(self, h, m, q):
        self.internal_caller(h)
        h.send(200, actions.manifest())

    # -- generic integration: state, files, upload, options, commands
    def get_integration_state(self, h, m, q):
        self.internal_caller(h)
        h.send(200, self.integration.state())

    def put_integration_options(self, h, m, q):
        self.internal_caller(h)
        h.send(200, self.notifier.set_options(h.json()))

    def get_files(self, h, m, q):
        self.internal_caller(h)
        h.send(200, {"files": self.integration.file_list()})

    def put_upload(self, h, m, q):
        self.internal_caller(h)
        try:
            h.send(200, files.store(self.media_dir, unquote(m.group(1)), h.rfile, content_length(h.headers),
                                    q.get("overwrite") == ["1"]))
        except files.UploadError as e:
            raise HttpError(e.status, str(e))

    def get_image(self, h, m, q):
        self.internal_caller(h)
        serial = m.group(1)
        png = self.cover_png(serial)
        if not png:
            raise NotFound("no preview")
        h.send(200, png, "image/png")

    def get_snapshot(self, h, m, q):
        """The camera's still image; a placeholder while the printer is offline, as a fetch could only fail then."""
        self.internal_caller(h)
        serial = m.group(1)
        printer = next((p for p in self.bridge_printers() if p.get("serial") == serial), None)
        if not printer:
            raise NotFound("unknown printer")
        if not self.integration.camera(serial, printer)["online"]:
            with open(OFFLINE_JPEG, "rb") as f:
                return h.send(200, f.read(), "image/jpeg")
        self.proxy(h, "/api/camera/%s/snapshot" % serial)

    def post_light(self, h, m, q):
        self.internal_caller(h)
        on = h.json().get("on")
        if not isinstance(on, bool):
            raise ValueError('Body must be {"on": true|false}.')
        h.send(200, self.bridge_ok("POST", "/api/light/" + m.group(1), {"on": on}, timeout=60))

    def post_rfid(self, h, m, q):
        self.internal_caller(h)
        b = h.json()
        h.send(200, self.bridge_ok("POST", "/api/rfid/" + m.group(1),
                                   {"ams_id": b.get("ams_id", 0), "slot_id": b.get("slot_id")}, timeout=60))

    def post_operation(self, run):
        def route(h, m, q):
            c = self.internal_caller(h)
            act_as = (h.headers.get(ACT_AS_HEADER) or "").strip()
            port = h.server.server_address[1]

            def call(method, path, body=None, data=None, binary=False, timeout=30):
                return self.local_call(port, act_as, method, path, body, data, binary, timeout)

            def print_file(fields):
                # Only an admin of the printer sends directly (as for the web UI); that includes force.
                serial = fields.get("serial") or fields.get("device_id")
                if act_as and self.access.role(c, serial) != "admin":
                    raise Forbidden("rule: only an admin of the printer can print directly; use the queue")
                return self.sender.print_file(fields)
            try:
                result = run(m.group(1), h.json(), actions.Ops(call, self.media_dir, print_file))
            except actions.ActionError as e:
                raise HttpError(e.status, str(e))
            h.send(200, {"result": result})
        return route

    def local_call(self, port, act_as, method, path, body, data, binary, timeout):
        """Ask this app's own route as the act-as user (none: the admin automation caller)."""
        headers = {SECRET_HEADER: self.secret}
        if act_as:
            headers[ACT_AS_HEADER] = act_as
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request(method, path, data, headers)
            r = conn.getresponse()
            raw = r.read()
        except OSError as e:
            raise actions.ActionError(502, "the app is not reachable: %s" % e)
        finally:
            conn.close()
        if r.status >= 300:
            try:
                text = json.loads(raw)["error"]
            except (ValueError, KeyError, TypeError):
                text = raw.decode(errors="replace")[:300]
            raise actions.ActionError(r.status, text)
        return raw if binary else json.loads(raw or b"null")

    def bridge_route(self, h, m, q):
        self.need(self.caller(h), "ha_admin")
        self.proxy(h, "/" + m.group(1), urlsplit(h.path).query)

    # -- mounting --
    def routes(self):
        from queue_routes import make_routes
        w = self.wrap
        mine = [
            ("GET", r"/api/me", w(self.get_me)),
            ("GET", r"/api/setup", w(self.get_setup, "ha_admin")),
            ("POST", r"/api/setup/login/start", w(self.login("start"), "ha_admin")),
            ("POST", r"/api/setup/login/code", w(self.login("code"), "ha_admin")),
            ("DELETE", r"/api/setup/accounts/([a-z0-9_]{1,64})", w(self.delete_account, "ha_admin")),
            ("PUT", r"/api/setup/printers/([A-Za-z0-9_-]{1,40})", w(self.put_printer, "ha_admin")),
            ("DELETE", r"/api/setup/printers/([A-Za-z0-9_-]{1,40})", w(self.delete_printer, "ha_admin")),
            ("GET", r"/api/integration", w(self.get_integration, "ha_admin")),
            ("GET", r"/api/printers", w(self.get_printers)),
            ("GET", r"/api/printers/([^/]+)/camera", lambda h, m, q: self.get_camera(h, m, q, self.caller(h))),
            ("GET", r"/api/printers/([^/]+)/cover", lambda h, m, q: self.get_cover(h, m, q, self.caller(h))),
            ("GET", r"/api/queue/" + JOB_ID + r"/plate\.png", lambda h, m, q: self.get_plate(h, m, q, self.caller(h))),
            ("POST", r"/api/queue", w(self.post_queue)),
            ("POST", r"/api/printers/([^/]+)/drying", w(self.post_drying)),
            ("POST", r"/api/printers/([^/]+)/drying/stop", w(self.post_drying_stop)),
            ("POST", r"/api/printers/([^/]+)/light", w(self.post_printer_light)),
            ("POST", r"/api/jobs", self.post_job),
            ("GET", r"/api/jobs/" + JOB_ID, self.get_slice),
            ("POST", r"/api/jobs/" + JOB_ID + r"/gcode", self.slicer_job(self.s.post_gcode, self.check_gcode)),
            ("GET", r"/api/engine/filament", self.get_engine_filament),
            ("GET", r"/api/jobs/" + JOB_ID + r"/(plate\.png|plate\.gcode)", self.slicer_job(self.s.get_file)),
            ("GET", r"/api/jobs/" + JOB_ID + r"/model", self.slicer_job(self.s.get_model)),
            ("POST", r"/api/jobs/" + JOB_ID + r"/model", self.slicer_job(self.s.post_model_part)),
            ("POST", r"/api/jobs/" + JOB_ID + r"/print", self.slicer_job(self.s.post_print, self.check_print)),
            ("POST", r"/api/jobs/" + JOB_ID + r"/queue", w(self.queue_job)),
            ("GET", r"/api/history", w(self.get_history)),
            ("GET", r"/api/history/([A-Za-z0-9_-]{1,40})/settings", w(self.get_history_settings)),
            ("GET", r"/api/history/([A-Za-z0-9_-]{1,40})/file", w(self.get_history_file)),
            ("GET", r"/api/history/([A-Za-z0-9_-]{1,40})/gcode", w(self.get_history_gcode)),
            ("POST", r"/api/history/([A-Za-z0-9_-]{1,40})/queue", w(self.post_history_queue)),
            ("GET", r"/api/spools", w(self.get_spools)),
            ("GET", r"/api/spools/(\d{1,19})/prints", w(self.get_spool_prints)),
            ("POST", r"/api/spools", w(self.post_spool)),
            ("PUT", r"/api/spools/(\d{1,19})", w(self.put_spool)),
            ("DELETE", r"/api/spools/(\d{1,19})", w(self.delete_spool)),
            ("POST", r"/api/spools/(\d{1,19})/load-request", w(self.post_load_request)),
            ("GET", r"/api/load-requests", w(self.get_load_requests)),
            ("POST", r"/api/printers/" + SERIAL + r"/nozzle-request", w(self.post_nozzle_request)),
            ("GET", r"/api/nozzle-requests", w(self.get_nozzle_requests)),
            ("DELETE", r"/api/nozzle-requests/([0-9a-f]{12})", w(self.delete_nozzle_request)),
            ("DELETE", r"/api/load-requests/([0-9a-f]{12})", w(self.delete_load_request)),
            ("GET", r"/api/ha/users", w(self.get_ha_users, "ha_admin")),
            ("GET", r"/api/ha/entities", w(self.get_ha_entities, "ha_admin")),
            ("POST", r"/api/ha/switches", w(self.create_switch, "ha_admin")),
            ("POST", r"/api/feedback", w(self.post_feedback)),
            ("GET", r"/api/events", w(self.get_events, "internal")),
            ("GET", r"/api/integration/manifest", self.get_manifest),
            ("GET", r"/api/integration/state", self.get_integration_state),
            ("PUT", r"/api/integration/options", self.put_integration_options),
            ("GET", r"/api/integration/printers/" + SERIAL + r"/image", self.get_image),
            ("GET", r"/api/integration/printers/" + SERIAL + r"/snapshot", self.get_snapshot),
            ("POST", r"/api/integration/printers/" + SERIAL + r"/light", self.post_light),
            ("POST", r"/api/integration/printers/" + SERIAL + r"/rfid", self.post_rfid),
            ("GET", r"/api/files", self.get_files),
            ("PUT", r"/api/upload/([^/]+)", self.put_upload),
            ("POST", r"/api/actions/([a-z_]+)", self.post_operation(actions.run_action)),
            ("POST", r"/api/tools/([a-z_]+)", self.post_operation(actions.run_tool)),
            ("GET", r"/api/grants", w(self.get_grants)),
            ("POST", r"/api/grants", w(self.post_grants)),
            # The bridge's own page and API (account login), for HA admins only.
            ("GET", r"/api/bridge/(.*)", self.bridge_route),
            ("POST", r"/api/bridge/(.*)", self.bridge_route),
            ("DELETE", r"/api/bridge/(.*)", self.bridge_route),
        ]
        if self.ui_dir:
            mine += [("GET", "/", self.get_ui), ("GET", "/" + UI_FILES, self.get_ui)]
        return mine + make_routes(self.queue, self.access, self.is_internal, self.caller)

    def install(self):
        """Replace the slicer's overlapping routes with ours, check identity first, add PUT and DELETE."""
        s, mine = self.s, self.routes()
        taken = {(meth, rx) for meth, rx, _ in mine}
        s.ROUTES[:] = mine + [r for r in s.ROUTES if (r[0], r[1]) not in taken and r[:2] != ("GET", "/api/printers")]
        s.PRINT = self.sender.print_file
        s.Handler.dispatch = lambda h, method: self.dispatch(h, method, s.ROUTES)
        s.Handler.do_PUT = lambda h: h.dispatch("PUT")
        s.Handler.do_DELETE = lambda h: h.dispatch("DELETE")
