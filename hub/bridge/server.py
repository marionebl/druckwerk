#!/usr/bin/env python3
"""Druckwerk bridge: HTTP front-end (login page + JSON API) in front of the boocloud-bridge daemon.

One boocloud-bridge daemon per cloud account (the plugin holds one login per process).
Routes: GET /, GET /api/health, POST /api/login/start, POST /api/login/code, DELETE /api/accounts/<id>,
GET /api/printers, GET /api/status/<serial> (cached 5 s, last good answer on failure, header X-Status-Age), GET /api/ams/<serial>, GET /api/camera/<serial>/live, GET /api/tasks?serial=<serial>&limit=<n>, POST /api/print, GET /api/print-progress/<serial>,
GET /api/camera/<serial>/snapshot (cached 30 s, header X-Snapshot-Age), GET|POST /api/filament?account=<id>, PUT|DELETE /api/filament/<id>?account=<id> (the account's cloud filament library),
GET /api/drying/<serial>, POST /api/ams/<serial>/drying, POST /api/ams/<serial>/drying/stop.
"""
import base64
import binascii
import contextlib
from datetime import datetime
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.dirname(HERE)):  # python -I drops the script dir; the repo keeps externals.py one level up
    sys.path.insert(0, _d)
import externals  # noqa: E402
import camera  # noqa: E402
from httplen import content_length  # noqa: E402

env = os.environ.get
DATA = env("DW_DATA", "/data")
API_BASE = env("DW_API_BASE", externals.CLOUD_API_BASE)
PORT_BASE = int(env("DW_DAEMON_PORT_BASE", "8765"))
CERT_SRC = env("DW_CERT", "/opt/bridge/slicer_base64.cer")
DAEMON_CMD = shlex.split(env("DW_DAEMON_CMD", "/bridge.sh"))
BACKOFF_MIN = float(env("DW_BACKOFF_MIN", "5"))
BACKOFF_MAX = float(env("DW_BACKOFF_MAX", "60"))
# A camera URL timeout alone does not restart the daemon. It marks the printer as suspect. The monitor then asks the printer
# for a full report (pushall) and restarts the daemon only if the status does not change within PROBE_WAIT seconds, or
# /health fails for HEALTH_BAD seconds. An idle printer sends no status by itself, so passive staleness proves nothing.
# Mint timeouts are common in the first minutes after a daemon login, so a timeout within START_GRACE of a start is ignored.
STATUS_SAMPLE = float(env("DW_STATUS_SAMPLE", "5"))  # seconds between monitor ticks
PROBE_WAIT = float(env("DW_PROBE_WAIT", "15"))
HEALTH_BAD = float(env("DW_HEALTH_BAD", "60"))
START_GRACE = float(env("DW_START_GRACE", "180"))
TERM_WAIT = float(env("DW_TERM_WAIT", "10"))  # seconds the daemon gets to end after SIGTERM before it is killed
RESTART_BACKOFF = float(env("DW_RESTART_BACKOFF", "300"))  # least seconds between two restarts of this kind
SUSPECT_TTL = float(env("DW_SUSPECT_TTL", "600"))  # a camera timeout is dropped after this long without a restart
# /status: the daemon asks the printer for a full push on every call that finds its cache older than 10 s, and an idle printer
# sends no updates, so calls queue up and time out. One call at a time per printer, answers under 5 s old are reused, and a
# failed call is answered with the last good status for up to 10 min.
STATUS_CACHE = float(env("DW_STATUS_CACHE", "5"))
STATUS_TIMEOUT = float(env("DW_STATUS_TIMEOUT", "8"))  # under the hub's 10 s, so the hub gets our answer, not its own timeout
STATUS_STALE = float(env("DW_STATUS_STALE", "600"))
PRINT_DAEMON_TIMEOUT = float(env("DW_PRINT_TIMEOUT", "280"))  # seconds; the app waits 300 s, so the bridge answers first
RETRY_WINDOW = float(env("DW_RETRY_WINDOW", "60"))  # seconds a print start or command waits for a daemon that is not up
MAX_PRINT_BODY = 50 * 1024 * 1024
MAX_SMALL_BODY = 8192
ACCOUNTS = os.path.join(DATA, "accounts")
plugin = None  # set in main when the image fetches the vendor plugin at start (hub/bridge/plugin.py)
SLICER_HEADERS = externals.CLOUD_HEADERS  # the cloud checks the client name
SERIAL = re.compile(r"[A-Za-z0-9_-]{1,64}")

secrets_seen = set()  # token strings, masked in everything we log
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy


def log(message):
    for s in secrets_seen:
        message = message.replace(s, "***")
    print("druckwerk-bridge: " + message, file=sys.stderr, flush=True)


class Upstream(Exception):
    """The cloud or the daemon did not answer. refused: the connection was refused, so nothing was sent."""

    def __init__(self, *args, refused=False):
        super().__init__(*args)
        self.refused = refused


def call(method, url, headers=None, body=None, timeout=15):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "application/json")
    except (OSError, ValueError) as e:
        refused = isinstance(e, ConnectionRefusedError) or isinstance(getattr(e, "reason", None), ConnectionRefusedError)
        raise Upstream(type(e).__name__, refused=refused)


def read_cloud(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f).get("cloud", {})
    except (OSError, ValueError):
        return {}


def write_private(path, text):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ---- accounts and daemon supervision ----

accounts = {}  # id -> Account
lock = threading.RLock()  # guards the accounts and pending dicts only; never held during a cloud call
login_locks = {}  # account id -> Lock: two logins of one account run one after the other


def slug(email):
    return re.sub(r"[^a-z0-9]+", "_", email.lower()).strip("_")[:64]


def pick_port():
    used = {a.port for a in accounts.values()}
    return next(p for p in range(PORT_BASE, PORT_BASE + 1000) if p not in used)


def install_cert(home):
    dest = os.path.join(home, ".local/share/bambox/cert/slicer_base64.cer")  # where the bridge looks first
    if os.path.exists(CERT_SRC) and not os.path.exists(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(CERT_SRC, dest)


ANSI = re.compile(r"\x1b\[[0-9;]*m")
PROGRESS = re.compile(r'print progress\s+stage="(\w+)"\s+code=(-?\d+)')


def parse_progress(line):
    """{stage, percent} from the daemon's "print progress" log line (tracing may colour it); percent is only set for Upload."""
    m = PROGRESS.search(ANSI.sub("", line))
    if not m:
        return None
    stage, code = m.group(1).lower(), int(m.group(2))
    return {"stage": stage, "percent": code if stage == "upload" and 0 <= code <= 100 else None}


class Account:
    def __init__(self, id, email):
        self.id, self.email = id, email
        self.dir = os.path.join(ACCOUNTS, id)
        self.creds = os.path.join(self.dir, "credentials.toml")
        self.printers = os.path.join(self.dir, "printers.json")
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        portfile = os.path.join(self.dir, "port")
        try:
            with open(portfile) as f:
                self.port = int(f.read())
        except (OSError, ValueError):
            self.port = pick_port()
            write_private(portfile, str(self.port))  # kept so the port stays stable across restarts
        self.daemon = None
        self.upload = None  # {serial, stage, percent} of the print being sent, from the daemon's log

    def url(self, path):
        return "http://%s:%d%s" % (env("DW_DAEMON_HOST", "127.0.0.1"), self.port, path)

    def note_progress(self, line):
        """The daemon only logs the vendor library's send stages; its code is the percent while stage is Upload."""
        up, found = self.upload, parse_progress(line)
        if up and found:
            up.update(found)

    def devices(self):
        try:
            with open(self.printers) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []


class Daemon:
    def __init__(self, acc):
        self.acc, self.proc, self.restarts = acc, None, 0
        self.dead = False
        self.wake = threading.Event()
        self.busy_lock, self.busy = threading.Lock(), 0  # print starts and commands in flight
        self.started = self.last_restart = 0.0
        self.suspect = None  # {serial, since, silent} of a camera URL timeout that still needs a look
        self.bad_since = None  # since when /health did not answer although the process runs
        threading.Thread(target=self.loop, daemon=True).start()
        threading.Thread(target=self.monitor, daemon=True).start()

    @contextlib.contextmanager
    def working(self):
        """Mark a print start or command as in flight: the monitor does not restart the daemon meanwhile."""
        with self.busy_lock:
            self.busy += 1
        try:
            yield
        finally:
            with self.busy_lock:
                self.busy -= 1

    def camera_timeout(self, serial):
        """The plugin may have stopped delivering status. The monitor probes the printer before it restarts anything."""
        age = time.time() - self.started
        if age < START_GRACE:
            log("[%s] camera timeout %.0fs after daemon start: within the grace period, ignored" % (self.acc.id, age))
            return
        log("[%s] camera timeout %.0fs after daemon start: will probe the printer status" % (self.acc.id, age))
        self.suspect = {"serial": serial, "since": time.time(), "silent": False}

    def stamp(self, serial):
        """(sequence_id, t_utc) of the cached status; None when the daemon does not answer."""
        try:
            status, raw, _ = call("GET", self.acc.url("/status/" + serial), timeout=3)
            p = json.loads(raw).get("print", {}) if status == 200 else None
        except (Upstream, ValueError, AttributeError):
            return None
        return (p.get("sequence_id"), p.get("t_utc")) if isinstance(p, dict) else None

    def probe(self, serial):
        """Ask the printer for a full report; seconds until the cached status changed, None if it did not within PROBE_WAIT."""
        began = time.time()
        before = self.stamp(serial)
        try:
            call("POST", self.acc.url("/command/" + serial), {"Content-Type": "application/json"},
                 json.dumps({"pushing": {"sequence_id": "0", "command": "pushall", "version": 1, "push_target": 1}}).encode(), timeout=15)
        except Upstream:
            return None
        end = began + PROBE_WAIT
        while time.time() < end and not self.dead:
            now = self.stamp(serial)
            if now is not None and now != before:
                return time.time() - began
            time.sleep(min(0.5, PROBE_WAIT / 10))
        return None

    def sample(self, now):
        if not self.running() or daemon_json(self.acc, "/health", 3) is not None:
            self.bad_since = None
        elif self.bad_since is None:
            self.bad_since = now

    def check(self, now):
        s = self.suspect
        if not s:
            return
        if now - s["since"] > SUSPECT_TTL:
            self.suspect = None
            return
        dead = self.bad_since is not None and now - self.bad_since > HEALTH_BAD
        if now - self.last_restart < RESTART_BACKOFF or self.busy:
            return  # wait; a print start or command runs, or the last restart was recent
        if not dead and not s["silent"]:
            answered = self.probe(s["serial"])
            if answered is not None:
                log("[%s] camera timeout probe: status answered in %.1fs, no restart" % (self.acc.id, answered))
                self.suspect = None
                return
            log("[%s] camera timeout probe: no status in %.0fs, restarting" % (self.acc.id, PROBE_WAIT))
            s["silent"] = True
        with self.busy_lock:
            if self.busy:
                return  # look again at the next tick
            self.suspect, self.last_restart = None, now
            log("[%s] %s, restarting daemon" % (self.acc.id, "daemon does not answer /health" if dead
                                              else "printer status did not change after a probe"))
            self.restart()

    def monitor(self):
        while not self.dead:
            time.sleep(STATUS_SAMPLE)
            if self.dead:
                return
            try:
                now = time.time()
                self.sample(now)
                self.check(now)
            except Exception as e:  # the monitor must not die
                log("[%s] monitor: %s" % (self.acc.id, type(e).__name__))

    def restart(self):
        """Make the daemon reread the credentials."""
        self.wake.set()
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            # A daemon that ignores SIGTERM would keep the old credentials for good.
            t = threading.Timer(TERM_WAIT, lambda: p.poll() is None and p.kill())
            t.daemon = True
            t.start()

    def stop(self):
        self.dead = True
        self.wake.set()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def loop(self):
        delay = BACKOFF_MIN
        tag = "[%s] " % self.acc.id
        while not self.dead:
            if plugin:
                plugin.ready.wait()  # a daemon without the plugin would only crash-loop
            began = time.time()
            try:
                install_cert(self.acc.home)
                self.proc = subprocess.Popen(DAEMON_CMD + [self.acc.creds, str(self.acc.port)],
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                             errors="replace", env=dict(camera.clean_env(), HOME=self.acc.home))
                self.started, self.bad_since = time.time(), None
                if self.dead:  # stop() ran before the process existed
                    self.proc.kill()
                for line in self.proc.stdout:
                    log(tag + "daemon: " + line.rstrip())
                    self.acc.note_progress(line)
                    # Under qemu an abort leaves the process hanging instead of exiting.
                    if line.startswith(("error:", "terminate called", "qemu: uncaught")):
                        self.proc.kill()
                code = self.proc.wait()
            except OSError as e:
                code = "start failed: %s" % type(e).__name__
            except Exception as e:  # noqa: BLE001 - the loop is the only restarter of this daemon
                code = "loop error: %s" % type(e).__name__
                log(tag + code)
                if self.proc and self.proc.poll() is None:
                    self.proc.kill()
                    self.proc.wait()
            if self.dead:
                return
            if self.wake.is_set():  # restart requested by a new login
                self.wake.clear()
                delay = BACKOFF_MIN
                continue
            self.restarts += 1
            if time.time() - began > BACKOFF_MAX:  # it ran for a while, so this is a fresh failure
                delay = BACKOFF_MIN
            log(tag + "daemon exited (%s), restarting in %.1fs" % (code, delay))
            if self.wake.wait(delay):
                self.wake.clear()
                delay = BACKOFF_MIN
            else:
                delay = min(delay * 2, BACKOFF_MAX)

    def running(self):
        return self.proc is not None and self.proc.poll() is None


def add_account(id, email):
    acc = accounts[id] = Account(id, email)
    acc.daemon = Daemon(acc)
    return acc


def migrate_v01():
    """v0.1 kept one account in /data; move it into an account dir once."""
    old_creds, old_printers = os.path.join(DATA, "credentials.toml"), os.path.join(DATA, "printers.json")
    if not os.path.exists(old_creds):
        return
    id = slug(read_cloud(old_creds).get("email", "")) or "legacy"
    d = os.path.join(ACCOUNTS, id)
    os.makedirs(os.path.join(d, "home"), mode=0o700, exist_ok=True)
    # Credentials move last: if we crash before that, the next start repeats the whole move.
    for old, name in ((old_printers, "printers.json"), (old_creds, "credentials.toml")):
        if os.path.exists(old):
            os.chmod(old, 0o600)
            os.replace(old, os.path.join(d, name))
    log("migrated v0.1 login to account " + id)


def load_accounts():
    migrate_v01()
    for id in sorted(os.listdir(ACCOUNTS)) if os.path.isdir(ACCOUNTS) else []:
        cloud_cfg = read_cloud(os.path.join(ACCOUNTS, id, "credentials.toml"))
        if cloud_cfg.get("token"):
            secrets_seen.update(v for k, v in cloud_cfg.items() if k in ("token", "refresh_token") and v)
            add_account(id, cloud_cfg.get("email", id))


# ---- login ----

pending = {}  # lower-case email -> email and, for the 2FA step, the tfaKey; the password is never kept


def cloud(method, path, body=None, token=None, parse=True):
    headers = dict(SLICER_HEADERS)
    if token:
        headers["Authorization"] = "Bearer " + token
    status, raw, _ = call(method, API_BASE + path, headers, None if body is None else json.dumps(body).encode())
    if status >= 400:
        raise Upstream("%s cloud answered HTTP %d for %s" % (externals.CLOUD_NAME, status, path))
    if not parse:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise Upstream("%s cloud sent invalid JSON for %s" % (externals.CLOUD_NAME, path))


def login_lock(email):
    with lock:
        return login_locks.setdefault(slug(email), threading.Lock())


def advance(email, data):
    """Look at a login answer: finish, or say which code is needed next."""
    if data.get("accessToken"):
        finish(email, data["accessToken"], data.get("refreshToken", ""))
        return "done"
    if data.get("tfaKey"):
        with lock:
            pending[email.lower()] = {"email": email, "kind": "tfa", "tfaKey": data["tfaKey"]}
        return "need_tfa"
    if data.get("loginType") == "verifyCode":
        with lock:
            pending[email.lower()] = {"email": email, "kind": "code"}
        # The real cloud answers this call without a JSON body (upstream auth.py checks only the status).
        cloud("POST", "/v1/user-service/user/sendemail/code", {"email": email, "type": "codeLogin"}, parse=False)
        return "need_code"
    raise Upstream("Login failed: no access token in the answer")


def finish(email, token, refresh):
    id = slug(email)
    with lock:
        if id in accounts and accounts[id].email.lower() != email.lower():
            raise ValueError("Another account already uses the id " + id)
    secrets_seen.update(s for s in (token, refresh) if s)
    uid = str(cloud("GET", "/v1/design-user-service/my/preference", token=token).get("uid", ""))
    devices = [d for d in cloud("GET", "/v1/iot-service/api/user/bind", token=token).get("devices", []) if d.get("dev_id")]
    q = json.dumps  # a JSON string is a valid TOML basic string
    lines = ["[cloud]"] + ["%s = %s" % (k, q(v)) for k, v in
                           (("token", token), ("refresh_token", refresh), ("uid", uid), ("email", email))]
    used = set()
    for d in devices:
        slug_ = re.sub(r"[^a-z0-9]+", "_", str(d.get("name", "")).lower()).strip("_") or d["dev_id"].lower()
        if slug_ in used:
            slug_ += "_" + d["dev_id"].lower()
        used.add(slug_)
        lines += ["", "[printers.%s]" % slug_, "serial = %s" % q(d["dev_id"])]
    with lock:
        acc = accounts.get(id)
        known = acc is not None
        if not acc:
            os.makedirs(os.path.join(ACCOUNTS, id), exist_ok=True)
            acc = Account(id, email)
        write_private(acc.printers, json.dumps(devices))
        write_private(acc.creds, "\n".join(lines) + "\n")
        pending.pop(email.lower(), None)
        if not known:
            accounts[id] = acc
            acc.daemon = Daemon(acc)
    if known:
        acc.daemon.restart()


def login_start(email, password):
    if not slug(email):
        raise ValueError("That is not an email address.")
    with login_lock(email):
        return advance(email, cloud("POST", "/v1/user-service/user/login",
                                    {"account": email, "password": password, "apiError": ""}))


def login_code(code, email):
    with lock:
        key = email.lower() if email else next(iter(pending)) if len(pending) == 1 else None
        p = dict(pending.get(key) or {})
    if not p:
        raise ValueError("No pending login. Start it first, and send the email when several are pending.")
    if p["kind"] == "tfa":
        body, path = {"tfaKey": p["tfaKey"], "tfaCode": code}, "/v1/user-service/user/tfa"
    else:
        body, path = {"account": p["email"], "code": code}, "/v1/user-service/user/login"
    with login_lock(p["email"]):
        return p["email"], advance(p["email"], cloud("POST", path, body))


# ---- API ----

def private(device):
    return {k: v for k, v in device.items() if "access_code" not in k}  # LAN access code, not for clients


def owner(serial):
    with lock:
        return next((a for a in accounts.values() if any(d.get("dev_id") == serial for d in a.devices())), None)


def proxy(h, prefix, serial):
    acc = owner(serial)
    if not acc:
        return h.send(404, {"error": "unknown printer"})
    try:
        status, raw, ctype = call("GET", acc.url(prefix + serial))
    except Upstream:
        return h.send(503, {"error": "daemon not reachable"})
    h.send(status, raw, ctype)


class Stat:
    """The newest good status of one printer. Only one request at a time asks the daemon."""

    def __init__(self):
        self.lock = threading.Lock()
        self.raw = self.ctype = self.at = self.failure = None
        self.runs = 0


stats = {}  # serial -> Stat


def status(h, serial):
    acc = owner(serial)
    if not acc:
        return h.send(404, {"error": "unknown printer"})
    st = stats.setdefault(serial, Stat())
    seen = st.runs
    with st.lock:
        now = time.time()
        if st.raw and now - st.at < STATUS_CACHE:
            return h.send(200, st.raw, st.ctype, {"X-Status-Age": "%.1f" % (now - st.at)})
        if st.runs == seen:  # nobody asked while we waited: ask the daemon
            try:
                code, raw, ctype = call("GET", acc.url("/status/" + serial), timeout=STATUS_TIMEOUT)
                st.failure = (code, raw, ctype)
            except Upstream as e:
                st.failure = (503, json.dumps({"error": "daemon not reachable"}).encode(), "application/json")
                log("[%s] status %s: %s" % (acc.id, serial, e))
            st.runs += 1
            if st.failure[0] == 200:
                st.raw, st.ctype, st.at, st.failure = raw, ctype, time.time(), None
                return h.send(200, raw, ctype, {"X-Status-Age": "0.0"})
        # the call failed, or the request we waited for failed: the last good status, if it is not too old
        now = time.time()
        if st.raw and now - st.at < STATUS_STALE:
            return h.send(200, st.raw, st.ctype, {"X-Status-Age": "%.1f" % (now - st.at)})
        failure = st.failure
    h.send(*failure)


# Task status words we can justify from this repo (the printer's gcode_state names). The cloud's numeric codes are not
# documented here, so they pass through unmapped: add a number to this table once it is confirmed against a real answer.
TASK_STATUS = {"finish": "finished", "finished": "finished", "success": "finished", "failed": "failed", "fail": "failed",
               "cancel": "cancelled", "cancelled": "cancelled", "canceled": "cancelled", "running": "running", "printing": "running",
               # Live H2S history 2026-11-27 had only 2 (ran to the end) and 3 (ended early, failed or cancelled).
               "2": "finished", "3": "stopped"}


def epoch(value):
    """Seconds since 1970 from the cloud's ISO time or a number; None when absent or unreadable."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def normalize_task(t, serial):
    raw = t.get("status")
    status = TASK_STATUS.get(str(raw).strip().lower(), str(raw)) if raw is not None else "unknown"
    out = {"id": t.get("id"), "serial": serial, "title": t.get("title") or t.get("designTitle") or "", "status": status,
           "status_code": raw, "started_at": epoch(t.get("startTime")), "ended_at": epoch(t.get("endTime")),
           "seconds": number(t.get("costTime")), "grams": number(t.get("weight")), "cover_url": t.get("cover") or "",
           "plate": t.get("plateIndex"), "design_title": t.get("designTitle") or ""}
    # The ids that name the task's file in the cloud (the vendor slicer reads profileId from the same record).
    for key, cloud_key in (("model_id", "modelId"), ("profile_id", "profileId"), ("design_id", "designId")):
        if t.get(cloud_key) not in (None, ""):
            out[key] = t[cloud_key]
    return out


def get_tasks(h, m, body):
    q = parse_qs(urlsplit(h.path).query)
    serial = (q.get("serial") or [""])[0]
    if not SERIAL.fullmatch(serial):
        raise ValueError("serial is required")
    try:
        limit = int((q.get("limit") or ["50"])[0])
    except ValueError:
        limit = 0
    if not 1 <= limit <= 100:
        raise ValueError("limit must be a number from 1 to 100")
    acc = owner(serial)
    if not acc:
        return h.send(404, {"error": "unknown printer"})
    token = read_cloud(acc.creds).get("token")  # the token of the account that owns this printer
    if not token:
        raise Upstream("No cloud login for this printer's account")
    data = cloud("GET", "/v1/user-service/my/tasks?deviceId=%s&limit=%d" % (serial, limit), token=token)
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        raise Upstream("%s cloud sent no task list" % externals.CLOUD_NAME)
    h.send(200, [normalize_task(t, serial) for t in hits if isinstance(t, dict)])


def get_task_file(h, m, body):
    """The download URL of the sliced 3MF behind a task (its modelId and profileId); the caller fetches the file."""
    q = parse_qs(urlsplit(h.path).query)
    serial, model_id, profile_id = ((q.get(k) or [""])[0] for k in ("serial", "model_id", "profile_id"))
    if not (SERIAL.fullmatch(serial) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", model_id) and re.fullmatch(r"\d{1,19}", profile_id)):
        raise ValueError("serial, model_id and profile_id are required")
    acc = owner(serial)
    if not acc:
        return h.send(404, {"error": "unknown printer"})
    token = read_cloud(acc.creds).get("token")
    if not token:
        raise Upstream("No cloud login for this printer's account")
    data = cloud("GET", "/v1/iot-service/api/user/profile/%s?model_id=%s" % (profile_id, model_id), token=token)
    data = data if isinstance(data, dict) else {}
    listed = data.get("profile_print_3mf")
    url = data.get("url") or (listed[0].get("url") if isinstance(listed, list) and listed and isinstance(listed[0], dict) else None)
    if not isinstance(url, str) or not url.startswith("https://"):
        raise Upstream("%s cloud sent no file URL" % externals.CLOUD_NAME)
    h.send(200, {"url": url})


# ---- filament library (the cloud's "Filament Manager" spool list) ----

FILAMENT = "/v1/design-user-service/my/filament/v2"
FILAMENT_PAGE, FILAMENT_PAGES = 50, 20
# hub field -> cloud field; the cloud's names are camelCase (vendor slicer, wgtFilaManagerCloudSync).
SPOOL_FIELDS = {"brand": "filamentVendor", "type": "filamentType", "name": "filamentName",
                "grams_left": "netWeight", "grams_total": "totalNetWeight"}


def spool_list(data):
    """The spool array of a list answer; the vendor slicer accepts all of these shapes."""
    if isinstance(data, dict):
        for key in ("hits", "data", "filaments"):
            if key in data:
                return spool_list(data[key])
    return data if isinstance(data, list) else None


def normalize_spool(r, account):
    colour = r.get("color") or next(iter(r.get("colors") or []), "")
    unit, slot = r.get("amsId"), str(r.get("slotId") or "")
    # devId can outlive the spool's time in the printer, so only a spool that is in a printer counts as loaded.
    loaded = bool(r.get("devId")) and r.get("inPrinter") is not False
    return {"id": str(r.get("id")), "brand": r.get("filamentVendor") or "", "type": r.get("filamentType") or "",
            "name": r.get("filamentName") or "", "colour": re.sub(r"[^0-9A-Fa-f]", "", str(colour))[:6].upper(),
            "grams_left": number(r.get("netWeight")), "grams_total": number(r.get("totalNetWeight")),
            "printer": r["devId"] if loaded else None, "unit": number(unit) if loaded else None,
            "slot": int(slot) if loaded and slot.isdigit() else None, "in_printer": bool(r.get("inPrinter")),
            "rfid": r.get("RFID") or None, "account": account}


def spool_body(b, create):
    """Cloud request fields from the hub's spool fields; ValueError names what is wrong."""
    if not isinstance(b, dict):
        raise ValueError("Body must be a JSON object.")
    unknown = set(b) - set(SPOOL_FIELDS) - {"colour"}
    if unknown:
        raise ValueError("unknown spool fields: " + ", ".join(sorted(unknown)))
    out = {}
    for k, cloud_key in SPOOL_FIELDS.items():
        if k not in b:
            continue
        v = b[k]
        if k.startswith("grams"):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ValueError(k + " must be a number >= 0")
            v = int(round(v))
        elif not isinstance(v, str):
            raise ValueError(k + " must be text")
        out[cloud_key] = v
    if "colour" in b:
        c = str(b["colour"]).lstrip("#")
        if not re.fullmatch(r"[0-9A-Fa-f]{6}", c):
            raise ValueError("colour must be 6 hex digits")
        c = c.upper() + "FF"
        out.update(color=c, colors=[c], colorType=2)
    if create:
        for k in ("name", "colour"):
            if not b.get(k):
                raise ValueError(k + " is required")
        out.update(rolls=1, createType="manual", isSupport=False)
    return out


def filament_token(h):
    """(account, token) of the account named by ?account=; None after answering 404."""
    acc = accounts.get((parse_qs(urlsplit(h.path).query).get("account") or [""])[0])
    if not acc:
        h.send(404, {"error": "unknown account"})
        return None
    token = read_cloud(acc.creds).get("token")
    if not token:
        raise Upstream("No cloud login for this account")
    return acc, token


def get_filament(h, m, body):
    found = filament_token(h)
    if not found:
        return
    acc, token = found
    out = []
    for page in range(FILAMENT_PAGES):
        data = cloud("GET", "%s?offset=%d&limit=%d" % (FILAMENT, page * FILAMENT_PAGE, FILAMENT_PAGE), token=token)
        hits = spool_list(data)
        if hits is None:
            raise Upstream("%s cloud sent no filament list" % externals.CLOUD_NAME)
        out += [normalize_spool(r, acc.id) for r in hits if isinstance(r, dict)]
        if len(hits) < FILAMENT_PAGE:
            break
    h.send(200, out)


def post_filament(h, m, body):
    fields = spool_body(body, True)
    found = filament_token(h)
    if not found:
        return
    acc, token = found
    data = cloud("POST", FILAMENT, fields, token=token)
    # The create answer's shape is not verified: take a record if there is one, else the id alone.
    rec = data.get("data", data) if isinstance(data, dict) else {}
    rec = rec if isinstance(rec, dict) else {}
    h.send(200, normalize_spool(dict(fields, **rec), acc.id) if rec.get("id") is not None else {"id": None})


def put_filament(h, m, body):
    fields = spool_body(body, False)
    found = filament_token(h)
    if not found:
        return
    acc, token = found
    # The vendor API takes the id in the body, not the path.
    data = cloud("PUT", FILAMENT, dict(fields, id=int(m.group(1))), token=token)
    rec = data.get("data", data) if isinstance(data, dict) else {}
    h.send(200, normalize_spool(rec, acc.id) if isinstance(rec, dict) and rec.get("id") is not None else {"id": m.group(1)})


def delete_filament(h, m, body):
    found = filament_token(h)
    if not found:
        return
    cloud("DELETE", FILAMENT + "/batch", {"ids": [int(m.group(1))]}, token=found[1], parse=False)
    h.send(200, {"removed": m.group(1)})


def daemon_json(acc, path, timeout):
    try:
        status, raw, _ = call("GET", acc.url(path), timeout=timeout)
        return json.loads(raw) if status == 200 else None
    except (Upstream, ValueError):
        return None


def get_health(h, m, body):
    with lock:
        accs = list(accounts.values())
    local = h.client_address[0] in LOOPBACK  # the manual setup of the integration calls this from the network
    h.send(200, {"server": "ok", "plugin": plugin.status() if plugin else {"state": "ready"}, "accounts": [
        {"id": a.id, **({"email": a.email} if local else {}), "logged_in": True,
         "daemon": {"running": a.daemon.running(), "restarts": a.daemon.restarts,
                    "health": daemon_json(a, "/health", 3)}} for a in accs]})


def get_printers(h, m, body):
    with lock:
        accs = list(accounts.values())
    out = []
    for a in accs:
        answer = daemon_json(a, "/printers", 5)
        try:
            live = {p.get("serial"): p for p in answer.get("printers", [])}
        except AttributeError:
            live = {}
        out += [dict(private(d), serial=d["dev_id"], account=a.id, email=a.email, live=live.get(d["dev_id"]))
                for d in a.devices()]
    h.send(200, out)


def post_login_start(h, m, body):
    if not isinstance(body, dict) or not all(isinstance(body.get(k), str) and body[k] for k in ("email", "password")):
        return h.send(400, {"error": "Body must be JSON with email and password."})
    h.send(200, {"state": login_start(body["email"], body["password"]), "account": slug(body["email"])})


def post_login_code(h, m, body):
    if not isinstance(body, dict) or not isinstance(body.get("code"), str) or not body["code"]:
        return h.send(400, {"error": "Body must be JSON with a code."})
    if not isinstance(body.get("email", ""), str):
        return h.send(400, {"error": "email must be a string."})
    email, state = login_code(body["code"], body.get("email"))
    h.send(200, {"state": state, "account": slug(email)})


def delete_account(h, m, body):
    with lock:
        acc = accounts.pop(m.group(1), None)
        if not acc:
            return h.send(404, {"error": "unknown account"})
        pending.pop(acc.email.lower(), None)
        acc.daemon.stop()
        shutil.rmtree(acc.dir, ignore_errors=True)
    h.send(200, {"deleted": acc.id})


def multipart(fields):
    boundary = secrets.token_hex(16)
    out = b""
    for name, filename, ctype, value in fields:
        disp = 'form-data; name="%s"' % name + ('; filename="%s"' % filename if filename else "")
        out += ("--%s\r\nContent-Disposition: %s\r\nContent-Type: %s\r\n\r\n" % (boundary, disp, ctype)).encode()
        out += value + b"\r\n"
    return out + ("--%s--\r\n" % boundary).encode(), "multipart/form-data; boundary=" + boundary


# Send-time options of the vendor slicer's print dialog (PrintParams in bambu_networking.hpp, filled in SelectMachine.cpp).
# Tri-state options take true, false or "auto"; the daemon gets the slicer's pair: the bool is true only for "on" and the
# integer (0 off, 1 on, 2 auto) goes in auto_*. Defaults equal what the bridge sent before these options existed.
BED_TYPES = ("auto", "cool_plate", "eng_plate", "hot_plate", "textured_plate", "supertack_plate", "3d_effect_plate")


def _tri(body, key, default):
    v = body.get(key, default)
    if v is not True and v is not False and v != "auto":
        raise ValueError("%s must be true, false or \"auto\"." % key)
    return v


def _bool(body, key, default):
    v = body.get(key, default)
    if not isinstance(v, bool):
        raise ValueError("%s must be true or false." % key)
    return v


def print_options(body):
    p = {}
    for key, auto_key, default in (("bed_leveling", "auto_bed_leveling", True), ("flow_cali", "auto_flow_cali", True)):
        v = _tri(body, key, default)
        p[key] = v is True
        if v == "auto":
            p[auto_key] = 2
    p["vibration_cali"] = _bool(body, "vibration_cali", True)
    p["timelapse"] = _bool(body, "timelapse", False)
    p["use_ams"] = _bool(body, "use_ams", True)
    # The extra keys are left out at their default, so an older daemon gets the payload it always got.
    offset = _tri(body, "nozzle_offset_cali", False)
    if offset is not False:
        p["auto_offset_cali"] = 2 if offset == "auto" else 1
    if _bool(body, "layer_inspect", False):
        p["layer_inspect"] = True
    bed = body.get("bed_type", "auto")
    if bed not in BED_TYPES:
        raise ValueError("bed_type must be one of: " + ", ".join(BED_TYPES) + ".")
    if bed != "auto":
        p["bed_type"] = bed
    if _bool(body, "ext_change_assist", False):
        p["ext_change_assist"] = True
    return p


def call_daemon(acc, method, path, headers=None, body=None, timeout=15):
    """call() for a print start or command: marks it in flight, and while the daemon is not up (connection refused,
    nothing was sent) waits and tries again for up to RETRY_WINDOW seconds."""
    end = time.time() + RETRY_WINDOW
    with acc.daemon.working():
        while True:
            try:
                return call(method, acc.url(path), headers, body, timeout=timeout)
            except Upstream as e:
                if not e.refused or acc.daemon.dead or time.time() + 0.5 > end:
                    raise
            time.sleep(0.5)


def post_print(h, m, body):
    if not isinstance(body, dict):
        return h.send(400, {"error": "Body must be a JSON object."})
    serial, filename = body.get("serial"), body.get("filename")
    if not (isinstance(serial, str) and SERIAL.fullmatch(serial) and isinstance(filename, str) and filename):
        return h.send(400, {"error": "serial and filename are required."})
    try:
        raw = base64.b64decode(body.get("file_b64") or "", validate=True)
    except (binascii.Error, ValueError, TypeError):
        return h.send(400, {"error": "file_b64 is not valid base64."})
    if not raw:
        return h.send(400, {"error": "file_b64 is empty."})
    acc = owner(serial)
    if not acc:
        return h.send(404, {"error": "unknown printer"})
    filename = os.path.basename(filename)
    params = {"device_id": serial, "filename": filename, "project_name": re.sub(r"(\.gcode)?\.3mf$", "", filename)}
    params.update(print_options(body))
    data, ctype = multipart([("file", filename, "application/octet-stream", raw),
                             ("params", None, "application/json", json.dumps(params).encode())])
    acc.upload = {"serial": serial, "stage": "create", "percent": None}
    try:
        status, answer, actype = call_daemon(acc, "POST", "/print", {"Content-Type": ctype}, data, timeout=PRINT_DAEMON_TIMEOUT)
    except Upstream as e:
        if not e.refused:  # a timeout or a cut connection: the file may have reached the printer, the caller must not send it again
            return h.send(504, {"error": "daemon did not answer in time; the print may have started"})
        return h.send(503, {"error": "daemon not reachable"})
    finally:
        acc.upload = None
    h.send(status, answer, actype)


def get_print_progress(h, m, body):
    acc = owner(m.group(1))
    up = acc.upload if acc else None
    h.send(200, {"stage": up["stage"], "percent": up["percent"]} if up and up["serial"] == m.group(1) else {})


def send_command(h, acc, serial, payloads):
    for payload in payloads:
        try:
            status, raw, ctype = call_daemon(acc, "POST", "/command/" + serial, {"Content-Type": "application/json"},
                                             json.dumps(payload).encode(), timeout=30)
        except Upstream:
            return h.send(503, {"error": "daemon not reachable"})
        if status != 200:
            return h.send(status, raw, ctype)
    h.send(200, {"sent": len(payloads)})


def post_light(h, m, body):
    serial, acc = m.group(1), owner(m.group(1))
    if not acc:
        return h.send(404, {"error": "unknown printer"})
    if not isinstance(body, dict) or not isinstance(body.get("on"), bool):
        return h.send(400, {"error": "Body must be {\"on\": true|false}."})
    # Same payload as the vendor slicer's DevLamp; chamber_light2 is the H2 series' second light, others ignore it.
    payloads = [{"system": {"command": "ledctrl", "led_node": node, "sequence_id": "0",
                            "led_mode": "on" if body["on"] else "off", "led_on_time": 500,
                            "led_off_time": 500, "loop_times": 1, "interval_time": 1000}}
                for node in ("chamber_light", "chamber_light2")]
    send_command(h, acc, serial, payloads)


def post_rfid(h, m, body):
    serial, acc = m.group(1), owner(m.group(1))
    if not acc:
        return h.send(404, {"error": "unknown printer"})
    ams, slot = (body or {}).get("ams_id"), (body or {}).get("slot_id")
    if not (isinstance(ams, int) and 0 <= ams < 8 and isinstance(slot, int) and 0 <= slot < 4):
        return h.send(400, {"error": "Body must be {\"ams_id\": 0-7, \"slot_id\": 0-3}."})
    try:
        status, raw, _ = call("GET", acc.url("/status/" + serial), timeout=20)
        p = json.loads(raw).get("print", {}) if status == 200 else {}
    except (Upstream, ValueError):
        p = {}
    # The vendor slicer's check_enable_np: printers on the new protocol (H2 series) take ams_get_rfid,
    # the others an M620 R<tray> gcode line.
    if all(k in p for k in ("cfg", "fun", "aux", "stat")):
        payload = {"print": {"command": "ams_get_rfid", "sequence_id": "0", "ams_id": ams, "slot_id": slot, "mode": 0}}
    else:
        payload = {"print": {"command": "gcode_line", "sequence_id": "0", "param": "M620 R%d \n" % (ams * 4 + slot)}}
    send_command(h, acc, serial, [payload])


def post_pause(h, m, body):
    serial, acc = m.group(1), owner(m.group(1))
    if not acc:
        return h.send(404, {"error": "unknown printer"})
    # Same payload as the vendor slicer's command_task_pause and the vendor HA integration's PAUSE (which the firmware
    # ignores unsigned, hence this route).
    send_command(h, acc, serial, [{"print": {"command": "pause", "sequence_id": "0", "param": ""}}])


# AMS drying. Payloads: the vendor slicer's DevDryer::CtrlStartDrying / CtrlStopDrying (DevDryer.cpp). Unit type, status,
# ranges and the support flag: DevFilaSystem.cpp (info bits 0-3 type, 4-7 status), DevDryer.cpp (temperature ranges per type),
# DeviceManager.cpp (fun2 bit 5 is_support_remote_dry), AMSDryControl.cpp (1 to 72 hours).
DRYERS = {3: ("AMS 2 Pro", 45, 65), 4: ("AMS HT", 45, 85)}
DRY_STATUS = {0: "off", 1: "checking", 2: "drying", 3: "cooling", 4: "stopping", 5: "error", 6: "error", 7: "testing"}
DRY_ACTIVE = {"checking", "drying", "error"}  # the slicer's is_active_dry_status
DRY_HOURS = (1, 72)
# Highest drying temperature per filament family in the vendor filament presets (fdm_filament_*.json,
# filament_dev_ams_drying_temperature); a type not listed has no limit beyond the dryer's.
FILAMENT_MAX_TEMP = {"PLA": 55, "PETG": 65, "PET": 65, "ABS": 80, "ASA": 80, "PC": 80, "PA": 85, "TPU": 75}


def _hex(v):
    try:
        return int(str(v), 16)
    except ValueError:
        return None


def drying_units(status):
    """Units with a dryer in a daemon status: [{ams_id, name, status, active, left_min, filament, temp, hours, temp_min, temp_max}]."""
    p = (status or {}).get("print") or {}
    fun2 = _hex(p["fun2"]) if "fun2" in p else None
    if fun2 is not None and not fun2 >> 5 & 1:
        return []  # the printer does not support remote drying
    out = []
    units = p.get("ams", {}).get("ams", []) if isinstance(p.get("ams"), dict) else []
    for u in units:
        info = _hex(u.get("info")) if isinstance(u, dict) else None
        ams_id = int(u["id"]) if isinstance(u, dict) and str(u.get("id", "")).isdigit() else None
        if info is None or ams_id is None or info & 0xF not in DRYERS:
            continue
        name, lo, hi = DRYERS[info & 0xF]
        state = DRY_STATUS.get(info >> 4 & 0xF, "off")
        setting = u.get("dry_setting") if isinstance(u.get("dry_setting"), dict) else {}
        left = u.get("dry_time")
        out.append({"ams_id": ams_id, "name": name, "status": state, "active": state in DRY_ACTIVE,
                    "left_min": left if isinstance(left, int) else 0, "filament": setting.get("dry_filament") or "",
                    "temp": setting.get("dry_temperature") if state != "off" or setting.get("dry_temperature", -1) > 0 else None,
                    "hours": setting.get("dry_duration") if state != "off" or setting.get("dry_duration", -1) > 0 else None,
                    "temp_min": lo, "temp_max": hi})
    return out


def _ams_id(body):
    v = (body or {}).get("ams_id")
    if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v <= 3 or 128 <= v <= 135):
        raise ValueError("ams_id must be 0 to 3, or 128 to 135 for an AMS HT.")
    return v


def get_drying(h, m, body):
    serial, acc = m.group(1), owner(m.group(1))
    if not acc:
        return h.send(404, {"error": "unknown printer"})
    try:
        status, raw, _ = call("GET", acc.url("/status/" + serial), timeout=20)
        answer = json.loads(raw) if status == 200 else None
    except (Upstream, ValueError):
        answer = None
    if answer is None:
        return h.send(503, {"error": "no status from the printer"})
    h.send(200, drying_units(answer))


def post_drying(h, m, body):
    serial, acc = m.group(1), owner(m.group(1))
    if not acc:
        return h.send(404, {"error": "unknown printer"})
    if not isinstance(body, dict):
        return h.send(400, {"error": "Body must be a JSON object."})
    ams_id = _ams_id(body)
    temp, hours, filament = body.get("temp"), body.get("hours"), body.get("filament", "")
    if isinstance(temp, bool) or not isinstance(temp, int) or isinstance(hours, bool) or not isinstance(hours, int):
        raise ValueError("temp and hours must be whole numbers.")
    if not isinstance(filament, str) or len(filament) > 40:
        raise ValueError("filament must be text of at most 40 characters.")
    rotate, cooling = body.get("rotate_tray", False), body.get("cooling_temp", 50)
    if not isinstance(rotate, bool):
        raise ValueError("rotate_tray must be true or false.")
    if isinstance(cooling, bool) or not isinstance(cooling, int) or not 0 <= cooling <= 100:
        raise ValueError("cooling_temp must be 0 to 100.")
    if not DRY_HOURS[0] <= hours <= DRY_HOURS[1]:
        raise ValueError("hours must be %d to %d." % DRY_HOURS)
    try:
        status, raw, _ = call("GET", acc.url("/status/" + serial), timeout=20)
        answer = json.loads(raw) if status == 200 else None
    except (Upstream, ValueError):
        answer = None
    if answer is None:
        return h.send(503, {"error": "no status from the printer"})
    unit = next((u for u in drying_units(answer) if u["ams_id"] == ams_id), None)
    if not unit:
        raise ValueError("This printer reports no dryer on AMS unit %d." % ams_id)
    top = unit["temp_max"]
    family = next((k for k in sorted(FILAMENT_MAX_TEMP, key=len, reverse=True) if filament.upper().replace("-", " ").split(" ")[0].startswith(k)), None)
    if family:
        top = min(top, FILAMENT_MAX_TEMP[family])
    if not unit["temp_min"] <= temp <= top:
        raise ValueError("temp must be %d to %d for %s%s." % (unit["temp_min"], top, unit["name"], " with " + filament if family else ""))
    send_command(h, acc, serial, [{"print": {
        "command": "ams_filament_drying", "sequence_id": "0", "ams_id": ams_id, "zone_id": 0, "mode": 1,
        "filament": filament, "temp": temp, "duration": hours, "humidity": 0, "rotate_tray": rotate,
        "cooling_temp": cooling, "close_power_conflict": False}}])


def post_drying_stop(h, m, body):
    serial, acc = m.group(1), owner(m.group(1))
    if not acc:
        return h.send(404, {"error": "unknown printer"})
    send_command(h, acc, serial, [{"print": {
        "command": "ams_filament_drying", "sequence_id": "0", "ams_id": _ams_id(body), "mode": 0, "filament": "",
        "temp": 0, "duration": 0, "humidity": 0, "rotate_tray": False, "cooling_temp": 0,
        "close_power_conflict": False}}])


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Druckwerk bridge</title>
<style>body{font:16px system-ui,sans-serif;max-width:36rem;margin:2rem auto;padding:0 1rem}
input{width:100%;box-sizing:border-box;padding:.5rem;margin:.5rem 0}button{padding:.5rem 1rem}
li{margin:.5rem 0}</style></head>
<body><h1>Druckwerk bridge</h1>
<h2>Accounts</h2>
<ul id="accounts"></ul>
<h2>Add account</h2>
<form id="login"><input id="email" type="email" placeholder="Account email" autocomplete="username" required>
<input id="password" type="password" placeholder="Password" autocomplete="current-password" required>
<button>Log in</button></form>
<form id="codeform" hidden><label id="codelabel"></label><input id="code" autocomplete="one-time-code" required>
<button>Send code</button></form>
<p id="message"></p>
<script>
const $ = id => document.getElementById(id);
let pendingEmail = null;
async function call(path, method, body) {
  const r = await fetch(path, {method, body: body && JSON.stringify(body)});
  return [r.ok, await r.json()];
}
async function refresh() {
  const [, h] = await call("api/health", "GET");
  const [, p] = await call("api/printers", "GET");
  const ul = $("accounts");
  ul.replaceChildren();
  if (!h.accounts.length) ul.textContent = "No accounts yet.";
  for (const a of h.accounts) {  // textContent only: emails and printer names are not trusted HTML
    const names = p.filter(x => x.account === a.id).map(x => x.name || x.serial).join(", ") || "no printers";
    const li = document.createElement("li");
    li.textContent = a.email + ", printers: " + names + ", daemon " + (a.daemon.running ? "running" : "stopped") + " ";
    const b = document.createElement("button");
    b.textContent = "Remove";
    b.onclick = async () => { if (confirm("Remove " + a.email + "?")) handle(await call("api/accounts/" + a.id, "DELETE")); };
    li.append(b);
    ul.append(li);
  }
}
function handle([ok, d]) {
  $("message").textContent = ok ? "" : d.error;
  if (ok && d.state) {
    $("codeform").hidden = d.state === "done";
    $("codelabel").textContent = d.state === "need_tfa" ? "Two-factor code" : "Code from the email the cloud sent you";
    $("code").value = "";
  }
  refresh();
}
$("login").onsubmit = async e => { e.preventDefault();
  pendingEmail = $("email").value;
  handle(await call("api/login/start", "POST", {email: pendingEmail, password: $("password").value}));
  $("password").value = ""; };
$("codeform").onsubmit = async e => { e.preventDefault();
  handle(await call("api/login/code", "POST", {code: $("code").value, email: pendingEmail})); };
refresh();
</script></body></html>
"""


def get_page(h, m, body):
    h.send(200, PAGE, "text/html; charset=utf-8")


SER = r"([A-Za-z0-9_-]{1,64})"
LOOPBACK = ("127.0.0.1", "::1")  # the app proxies from here; the secret lives in the app
# Open to the network: go2rtc cannot send a header, and the integration's setup step reads the health.
REMOTE_OK = (r"/api/health", r"/api/camera/" + SER + r"/live")
# (method, regex) -> (handler, max body bytes)
ROUTES = [
    ("GET", r"/", get_page, 0),
    ("GET", r"/api/health", get_health, 0),
    ("GET", r"/api/printers", get_printers, 0),
    ("GET", r"/api/status/" + SER, lambda h, m, b: status(h, m.group(1)), 0),
    ("GET", r"/api/ams/" + SER, lambda h, m, b: proxy(h, "/ams/", m.group(1)), 0),
    ("GET", r"/api/drying/" + SER, get_drying, 0),
    ("GET", r"/api/camera/" + SER + r"/url", lambda h, m, b: camera.camera(h, "url", m.group(1)), 0),
    ("GET", r"/api/camera/" + SER + r"/live", lambda h, m, b: camera.get_live(h, m.group(1)), 0),
    ("GET", r"/api/camera/" + SER + r"/snapshot", lambda h, m, b: camera.camera(h, "snapshot", m.group(1)), 0),
    ("GET", r"/api/tasks", get_tasks, 0),
    ("GET", r"/api/tasks/file", get_task_file, 0),
    ("GET", r"/api/filament", get_filament, 0),
    ("POST", r"/api/filament", post_filament, MAX_SMALL_BODY),
    ("PUT", r"/api/filament/(\d{1,19})", put_filament, MAX_SMALL_BODY),
    ("DELETE", r"/api/filament/(\d{1,19})", delete_filament, 0),
    ("POST", r"/api/login/start", post_login_start, MAX_SMALL_BODY),
    ("POST", r"/api/login/code", post_login_code, MAX_SMALL_BODY),
    ("DELETE", r"/api/accounts/([a-z0-9_]{1,64})", delete_account, 0),
    ("POST", r"/api/print", post_print, MAX_PRINT_BODY),
    ("GET", r"/api/print-progress/" + SER, get_print_progress, 0),
    ("POST", r"/api/light/" + SER, post_light, MAX_SMALL_BODY),
    ("POST", r"/api/rfid/" + SER, post_rfid, MAX_SMALL_BODY),
    ("POST", r"/api/pause/" + SER, post_pause, MAX_SMALL_BODY),
    ("POST", r"/api/ams/" + SER + r"/drying", post_drying, MAX_SMALL_BODY),
    ("POST", r"/api/ams/" + SER + r"/drying/stop", post_drying_stop, MAX_SMALL_BODY),
]


class Handler(BaseHTTPRequestHandler):
    timeout = 30  # a client that stops sending must not hold a thread

    def send(self, status, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        try:
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):  # the client gave up before the answer
            self.close_connection = True
            log("client went away: %s %s" % (self.command, urlsplit(self.path).path))

    def dispatch(self, method):
        path = urlsplit(self.path).path
        for m, regex, fn, limit in ROUTES:
            match = m == method and re.fullmatch(regex, path)
            if not match:
                continue
            if regex not in REMOTE_OK and self.client_address[0] not in LOOPBACK:
                self.close_connection = True
                return self.send(403, {"error": "Only the app may use this route."})
            try:
                length = content_length(self.headers)
            except ValueError as e:
                self.close_connection = True  # the body stays unread
                return self.send(400, {"error": str(e)})
            if length > limit:
                self.close_connection = True  # the body stays unread
                return self.send(413, {"error": "Body too large (limit %d bytes)." % limit})
            try:
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    return self.send(400, {"error": "Body is not valid JSON."})
                return fn(self, match, body)
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            except Upstream as e:
                return self.send(502, {"error": str(e)})
        self.send(404, {"error": "not found"})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def log_message(self, fmt, *args):
        log("%s %s" % (self.command, urlsplit(self.path).path))  # path only, no query


def main():
    global plugin
    camera.srv = sys.modules[__name__]
    if env("DW_PLUGIN_LINK_DIR"):
        import plugin as plugin_mod
        plugin = plugin_mod.from_env()
    os.makedirs(ACCOUNTS, exist_ok=True)
    load_accounts()
    if camera.LIVE_KEEP:
        threading.Thread(target=camera.keeper, daemon=True).start()
    server = ThreadingHTTPServer((env("DW_HOST", "0.0.0.0"), int(env("DW_PORT", "8098"))), Handler)
    server.daemon_threads = True
    log("listening on port %d" % server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
