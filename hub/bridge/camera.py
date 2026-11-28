"""Druckwerk bridge camera: stills, and the live upstream of each printer fanned out to HTTP clients.

Moved out of server.py so that Live can be unit tested without the bridge process. server.py sets `srv` to itself
at import: the camera needs its log, call, owner and accounts."""
import http.client
import json
import os
import queue
import re
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

env = os.environ.get

LIVE_IDLE = float(env("DW_LIVE_IDLE", "90"))  # seconds an unused live upstream stays open for a quick reopen
LIVE_STALL = float(env("DW_LIVE_STALL", "2"))
# Keep one live upstream per printer open at all times (Mario, 2026-11-28): a viewer gets frames at once, and stills
# come from it. Cost on a Pi 4: about one core and the camera's bit rate per printer.
LIVE_KEEP = env("DW_LIVE_KEEP", "1") == "1"
KEEP_TICK = float(env("DW_KEEP_TICK", "5"))
KEEP_BACKOFF = (float(env("DW_KEEP_BACKOFF_MIN", "5")), float(env("DW_KEEP_BACKOFF_MAX", "120")))  # a gap this long between upstream reads counts as a stall
SNAPSHOT_CACHE = float(env("DW_SNAPSHOT_CACHE", "30"))  # seconds a good snapshot is served again

# Set by server.py (log, call, Upstream, owner, accounts). The default lets a test use Live alone.
srv = SimpleNamespace(log=lambda message: None)


def log(message):
    srv.log(message)


class Snap:
    """The newest good snapshot of one printer. Only one request at a time asks the daemon."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jpeg = self.at = self.failure = None
        self.runs = 0


snaps = {}  # serial -> Snap


def fetch_camera(acc, serial, kind, qs):
    """(status, body, content type) of the daemon's camera route; a keyframe is turned into a JPEG."""
    try:
        # Minting plus the first keyframe under qemu can take the bridge's full 45 s + 15 s budget.
        status, raw, ctype = srv.call("GET", acc.url("/camera/%s/%s%s" % (serial, kind, qs)), timeout=90)
    except srv.Upstream:
        return 503, json.dumps({"error": "daemon not reachable"}).encode(), "application/json"
    if status == 502 and b"waiting for camera URL callback" in raw:
        acc.daemon.camera_timeout(serial)
    if status == 200 and ctype.startswith("video/h264"):
        jpeg = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "h264", "-i", "-", "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-"],
                              input=raw, capture_output=True, timeout=60, env=clean_env())
        if jpeg.returncode or not jpeg.stdout:
            err = {"error": "could not decode keyframe", "stage": "decode", "detail": jpeg.stderr.decode(errors="replace")[-300:]}
            return 502, json.dumps(err).encode(), "application/json"
        return 200, jpeg.stdout, "image/jpeg"
    return status, raw, ctype


def snapshot(h, acc, serial, qs, fresh=False):
    """fresh: only a picture taken after this request came in (after a light switch, say), not the cached one."""
    snap = snaps.setdefault(serial, Snap())
    seen, asked = snap.runs, time.time()
    with snap.lock:
        now = time.time()
        if snap.jpeg and (snap.at >= asked if fresh else now - snap.at < SNAPSHOT_CACHE):
            return h.send(200, snap.jpeg, "image/jpeg", {"X-Snapshot-Age": "%.1f" % (now - snap.at)})
        if snap.runs != seen and snap.failure:
            return h.send(*snap.failure)  # the request we waited for failed: do not ask the daemon again at once
        status, raw, ctype = fetch_camera(acc, serial, "snapshot", qs)
        snap.runs += 1
        if status == 200 and ctype.startswith("image/jpeg"):
            snap.jpeg, snap.at, snap.failure = raw, time.time(), None
            return h.send(200, raw, ctype, {"X-Snapshot-Age": "0.0"})
        snap.failure = (status, raw, ctype)
    h.send(status, raw, ctype)


def camera(h, kind, serial):
    acc = srv.owner(serial)
    if not acc:
        return h.send(404, {"error": "unknown printer"})
    if kind == "snapshot":
        with live_lock:
            live = lives.get(serial)
        # While a live upstream is open the bridge answers 409 busy to a snapshot, so never ask it.
        if live and live.ready.wait(90) and not live.error and not live.closed:
            jpeg = live.frame_jpeg(newest=parse_qs(urlsplit(h.path).query).get("fresh", [""])[0] == "1")
            if jpeg:
                return h.send(200, jpeg, "image/jpeg", {"X-Snapshot-Age": "0.0"})
            return h.send(502, {"error": "could not decode keyframe", "stage": "decode"})
    # The printer's ipcam does not always say which remote transport it uses, so callers can force one.
    query = parse_qs(urlsplit(h.path).query)
    proto = query.get("protocols", [""])[0]
    qs = "?protocols=" + proto if re.fullmatch(r"[a-z,]{1,40}", proto) else ""
    if kind == "snapshot":
        return snapshot(h, acc, serial, qs, fresh=query.get("fresh", [""])[0] == "1")
    h.send(*fetch_camera(acc, serial, kind, qs))


# ---- live camera ----

TS_PACKET = 188
PAT_PID, PMT_PID = 0, 0x1000  # ffmpeg's mpegts muxer puts the PMT here


def clean_env():
    """The plugin and ffmpeg must not see SUPERVISOR_TOKEN or the other variables of the app."""
    return {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TZ") if k in os.environ}


def ffmpeg_jpeg(args, data):
    """First decoded frame of a stream on stdin, as JPEG; None if it did not decode."""
    out = subprocess.run(["ffmpeg", "-loglevel", "error"] + args + ["-i", "-", "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-"],
                         input=data, capture_output=True, timeout=60, env=clean_env())
    return out.stdout if out.stdout and not out.returncode else None


def nal_types(raw, most=40):
    """NAL unit types in an Annex-B H.264 stream, run-length coded: "7 8 5 1x14 ..." (5 IDR, 1 other slice, 6 SEI, 7 SPS, 8 PPS)."""
    out, i = [], raw.find(b"\x00\x00\x01")
    while i >= 0 and i + 3 < len(raw) and len(out) < most:
        t = raw[i + 3] & 0x1F
        if out and out[-1][0] == t:
            out[-1][1] += 1
        else:
            out.append([t, 1])
        i = raw.find(b"\x00\x00\x01", i + 3)
    return " ".join(str(t) if c == 1 else "%dx%d" % (t, c) for t, c in out) or "none"


class Live:
    """One upstream connection to the daemon for one printer, fanned out to any number of HTTP clients."""

    def __init__(self, acc, serial, qs, keep=False):
        self.acc, self.serial, self.qs, self.keep = acc, serial, qs, keep
        self.lock = threading.Lock()
        self.clients = set()
        self.closed = False
        self.ready = threading.Event()  # set on the first frame, or on failure
        self.error = None  # (status, body, content type) when the daemon refused
        self.ctype = self.out_type = None  # what the daemon sends, and what our clients get
        self.jpeg = None  # JPEG mode: newest part, whole
        self.gop = []  # H.264 mode: TS chunks since the last keyframe, so a joiner decodes at once
        self.psi = {}  # H.264 mode: newest PAT and PMT packet
        self.still = (None, None)  # H.264 mode: (first chunk of a GOP, its keyframe as JPEG), one decode per keyframe
        self.idle_timer = None
        self.still_lock = threading.Lock()  # one decode at a time, so parallel stills share it
        self.sock = self.proc = None
        self.t0, self.first, self.last_read = time.time(), None, None  # timing for the log line at close
        self.stalls, self.longest, self.bytes = 0, 0.0, 0
        self.marks = {}  # name -> seconds since t0, for the first-frame log line
        self.head = b""  # the first upstream bytes, to log which NAL units came before the first keyframe
        threading.Thread(target=self.run, daemon=True).start()

    # -- upstream --

    def run(self):
        try:
            # Minting fails now and then (the plugin never calls back); a second mint usually works at once.
            if self.open_upstream() == "mint timeout" and not self.closed:
                log("live %s: camera URL timed out, minting again" % self.serial)
                self.error = None
                self.open_upstream()
        finally:
            self.close()

    def open_upstream(self):
        """Stream from the daemon until it ends. "mint timeout" when no camera URL came."""
        try:
            conn = http.client.HTTPConnection(env("DW_DAEMON_HOST", "127.0.0.1"), self.acc.port, timeout=90)  # also ends a stream that stalls
            conn.connect()
            self.sock = conn.sock  # http.client drops its own handle once the response starts
            conn.request("GET", "/camera/%s/live%s" % (self.serial, self.qs))
            resp = conn.getresponse()
            self.mark("headers")
            self.ctype = resp.getheader("Content-Type", "")
            if resp.status != 200:
                self.error = (resp.status, resp.read(), self.ctype)
                if resp.status == 502 and b"waiting for camera URL callback" in self.error[1]:
                    self.acc.daemon.camera_timeout(self.serial)
                    return "mint timeout"
            elif self.ctype.startswith("video/h264"):
                self.out_type = "video/mp2t"
                self.run_h264(resp)
            elif self.ctype.startswith("multipart/x-mixed-replace"):
                self.out_type = self.ctype
                self.run_jpeg(resp)
            else:
                self.error = (502, b'{"error": "unexpected live stream type"}', "application/json")
        except (OSError, http.client.HTTPException, ValueError) as e:
            if not self.ready.is_set():
                self.error = (503, json.dumps({"error": "daemon not reachable: " + type(e).__name__}).encode(), "application/json")
        return None

    def run_h264(self, resp):
        try:
            rate = float(json.loads(resp.getheader("x-stream-info", "{}")).get("frame_rate"))
        except (TypeError, ValueError):
            rate = 0
        # The raw stream has no timestamps: stamp frames at the advertised rate (15 if unknown).
        # Short probing keeps the start fast. Stream copy needs a camera feed without B-frames.
        self.proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-probesize", "32", "-analyzeduration", "0",
             "-framerate", str(rate if 1 <= rate <= 60 else 15), "-f", "h264", "-i", "-",
             "-c", "copy", "-flush_packets", "1", "-f", "mpegts", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=clean_env())
        threading.Thread(target=self.read_ts, daemon=True).start()
        try:
            while True:
                data = resp.read1(65536)
                if not data:
                    break
                self.got(len(data))
                if len(self.head) < 262144 and self.first is None:
                    self.head += data
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass  # ffmpeg or the stream is gone; read_ts and close() finish up
        finally:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def read_ts(self):
        rest = b""
        fd = self.proc.stdout.fileno()
        while True:
            data = os.read(fd, 65536)
            if not data:
                self.close()
                self.proc.stdout.close()  # only this thread reads it, so closing here cannot race a read
                return
            self.mark("ts")
            data = rest + data
            n = len(data) // TS_PACKET * TS_PACKET
            chunk, rest = data[:n], data[n:]
            if not chunk:
                continue
            key = None
            for off in range(0, n, TS_PACKET):
                p = chunk[off:off + TS_PACKET]
                pid = (p[1] & 0x1F) << 8 | p[2]
                if pid in (PAT_PID, PMT_PID):
                    self.psi[pid] = p
                elif p[1] & 0x40 and p[3] & 0x20 and p[4] and p[5] & 0x40:  # payload start with random access flag: a keyframe
                    key = off
            with self.lock:
                if key is not None:
                    self.gop = [chunk[key:]]
                elif self.gop:
                    self.gop.append(chunk)
                self.broadcast(chunk)
            if key is not None:
                self.mark_ready()

    def run_jpeg(self, resp):
        while True:
            part = resp.readline()
            if not part:
                return
            if not part.startswith(b"--"):
                continue
            head, length = part, 0
            while True:
                line = resp.readline()
                head += line
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
                if line in (b"\r\n", b"\n", b""):
                    break
            body = resp.read(length)
            if len(body) < length:
                return
            tail = resp.readline()  # the CRLF after the body
            self.got(len(body))
            with self.lock:
                self.jpeg = body
                self.broadcast(head + body + tail)
            self.mark_ready()

    def mark(self, name):
        self.marks.setdefault(name, time.time() - self.t0)

    def got(self, n):
        self.mark("data")
        now = time.time()
        if self.first is not None and now - self.last_read > LIVE_STALL:
            self.stalls += 1
            self.longest = max(self.longest, now - self.last_read)
        self.last_read, self.bytes = now, self.bytes + n

    def mark_ready(self):
        if self.first is None:
            self.first = time.time() - self.t0
            log("live %s: first frame after %.1fs (%s; NAL types before it: %s)" % (
                self.serial, self.first, ", ".join("%s %.1fs" % kv for kv in self.marks.items()), nal_types(self.head)))
            self.head = b""
        self.ready.set()

    def broadcast(self, data):
        for c in list(self.clients):
            try:
                c.put_nowait(data)
            except queue.Full:
                self.drop(c)  # a client that cannot keep up must not hold memory
        if not self.clients:
            self.arm_idle()  # its handler may be stuck in a write and never call leave()

    def drop(self, c):
        self.clients.discard(c)
        try:
            while not c.empty():
                c.get_nowait()
        except queue.Empty:
            pass  # the client's handler emptied it first
        c.put_nowait(None)

    def arm_idle(self):
        """Close after LIVE_IDLE without clients. The caller holds self.lock."""
        if not self.closed and not self.keep and not self.idle_timer:
            self.idle_timer = threading.Timer(LIVE_IDLE, self.idle_close)
            self.idle_timer.daemon = True
            self.idle_timer.start()

    def idle_close(self):
        # A client may have joined after the timer fired, so decide under the locks join() takes, and leave lives in the same step.
        with live_lock, self.lock:
            if self.clients or self.closed:
                return
            if lives.get(self.serial) is self:
                del lives[self.serial]
        self.close()

    # -- clients --

    def join(self):
        """A queue of byte chunks for one client; None ends it. Starts at a keyframe or part boundary."""
        c = queue.Queue(maxsize=2048)
        with self.lock:
            if self.closed:
                c.put(None)
                return c
            if self.idle_timer:
                self.idle_timer.cancel()
                self.idle_timer = None
            if self.jpeg is not None:
                c.put(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(self.jpeg) + self.jpeg + b"\r\n")
            else:
                for chunk in list(self.psi.values()) + self.gop:
                    c.put_nowait(chunk)
            self.clients.add(c)
        return c

    def leave(self, c):
        with self.lock:
            self.clients.discard(c)
            if not self.clients:
                self.arm_idle()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.idle_timer:
                self.idle_timer.cancel()
            clients, self.clients = list(self.clients), set()
        with live_lock:
            if lives.get(self.serial) is self:
                del lives[self.serial]
        for c in clients:
            try:
                c.put_nowait(None)
            except queue.Full:
                pass
        if not self.ready.is_set() and not self.error:
            self.error = (502, b'{"error": "stream ended before the first frame"}', "application/json")
        if self.first is None:
            log("live %s: no frame, ended after %.1fs (%s)" % (self.serial, time.time() - self.t0, (self.error or (0,))[0]))
        else:
            log("live %s: ended after %.0fs, first frame after %.1fs, %.1f MB, %d stalls over %.0fs, longest gap %.1fs"
                % (self.serial, time.time() - self.t0, self.first, self.bytes / 1e6, self.stalls, LIVE_STALL, self.longest))
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)  # wakes the blocked read; the daemon then closes the cloud tunnel
            except OSError:
                pass
            self.sock.close()
        if self.proc:
            if self.proc.poll() is None:
                self.proc.kill()
            try:
                self.proc.wait(timeout=5)  # reap it: a killed child stays defunct until waited for
            except subprocess.TimeoutExpired:
                log("live %s: ffmpeg did not exit after kill" % self.serial)
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        self.ready.set()

    # -- snapshot --

    def frame_jpeg(self, newest=False):
        """The newest frame as JPEG (H.264: the last keyframe, whatever its age; newest: the last frame, which decodes
        everything since that keyframe); None if there is none or it did not decode."""
        with self.lock:
            if self.closed:
                return None
            jpeg, gop = self.jpeg, list(self.gop)
        if jpeg is not None:
            return jpeg
        if not gop:
            return None
        data = b"".join(list(self.psi.values()) + gop)
        if not newest:
            # With an always-open upstream every still comes from here, so decode each keyframe once.
            with self.still_lock:
                if self.still[0] is not gop[0]:
                    self.still = (gop[0], ffmpeg_jpeg(["-f", "mpegts"], data))
                return self.still[1]
        with tempfile.TemporaryDirectory() as d:  # -update 1 overwrites the file with each frame: the last one stays
            out = os.path.join(d, "last.jpg")
            r = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "mpegts", "-i", "-", "-update", "1", "-c:v", "mjpeg", out],
                               input=data, capture_output=True, timeout=60, env=clean_env())
            if r.returncode or not os.path.exists(out):
                return None
            with open(out, "rb") as f:
                return f.read()


lives = {}  # serial -> Live
live_lock = threading.Lock()


def keeper():
    """Keeps a live upstream open for each printer of each account (LIVE_KEEP); reopens a dropped one after a backoff."""
    retry = {}  # serial -> (not before, next backoff)
    while True:
        time.sleep(KEEP_TICK)
        for acc in list(srv.accounts.values()):
            # Upload and video both go through the one plugin under qemu: open nothing new while a print is sent.
            if not (acc.daemon and acc.daemon.running()) or acc.upload:
                continue
            for d in acc.devices():
                serial = d.get("dev_id")
                with live_lock:
                    live = lives.get(serial)
                if not serial or live:
                    if live and live.first is not None:
                        retry.pop(serial, None)  # it played, so the next drop retries after the shortest backoff
                    continue
                now = time.time()
                at, back = retry.get(serial, (0, KEEP_BACKOFF[0]))
                if now < at:
                    continue
                retry[serial] = (now + back, min(back * 2, KEEP_BACKOFF[1]))
                with live_lock:
                    if serial not in lives:
                        lives[serial] = Live(acc, serial, "", keep=True)


def get_live(h, serial):
    acc = srv.owner(serial)
    if not acc:
        return h.send(404, {"error": "unknown printer"})
    proto = parse_qs(urlsplit(h.path).query).get("protocols", [""])[0]
    qs = "?protocols=" + proto if re.fullmatch(r"[a-z,]{1,40}", proto) else ""
    with live_lock:
        live = lives.get(serial)
        if not live:
            live = lives[serial] = Live(acc, serial, qs)
        q = live.join()
    # Same budget as a snapshot: minting plus the first keyframe can take 60 s under qemu.
    if not live.ready.wait(100):
        live.close()
        return h.send(504, {"error": "no frame from the camera"})
    if live.error:
        return h.send(*live.error)
    h.send_response(200)  # HTTP/1.0: the body ends when we close
    h.send_header("Content-Type", live.out_type)
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.close_connection = True
    h.connection.settimeout(30)  # a viewer that stops reading must not hold this thread
    try:
        while True:
            chunk = q.get()
            if chunk is None:
                break
            h.wfile.write(chunk)
            h.wfile.flush()
    except OSError:
        pass  # the viewer left
    finally:
        live.leave(q)
