#!/usr/bin/env python3
"""Slicer server: serves the browser slicer (WASM engine and presets), stores the G-code the browser made, sends it to a printer.

The app does not slice. The browser does, with the OrcaSlicer WASM engine (hub/slicer/engine).
Printers and AMS trays come from the bridge. A print goes through the app's own send (PRINT), so its busy check
and G-code guard apply. Run alone, it calls Home Assistant's druckwerk.print_file service instead.
"""
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "queue")):  # python -I drops the script dir; the repo keeps externals.py one level up
    sys.path.insert(0, _d)
import externals  # noqa: E402
from httplen import content_length  # noqa: E402
import pack  # noqa: E402
from queue_registry import write_json  # noqa: E402

env = os.environ.get
JOBS = env("SLICER_JOBS", "/data/jobs")
SHARE = env("SLICER_SHARE", "/share/druckwerk")
SENDER = env("SLICER_SENDER_URL", "http://127.0.0.1:8098")
CORE = env("SLICER_CORE_URL", "http://supervisor/core/api")
PRINT = None  # the app sets a function that sends a print; without it the print goes through the HA service
ENGINE = env("SLICER_ENGINE", "/srv/engine")  # OrcaSlicer WASM runtime and presets/<model>.json (see engine/build.sh)
MAX_MODEL = 100 * 1024 * 1024
MAX_PART = 1024 * 1024  # largest body of one part
KEEP_JOBS = 10
JOB_ID = r"([0-9a-f]{12})"

# Preset names per printer model (dev_product_name) live in externals.py. Quality = process profile.
PRINTERS = externals.PRINTERS


def printer_cfg(model, nozzle="0.4"):
    """Presets of a model for a nozzle diameter ("0.4" is the model's own entry); None when there are none."""
    cfg = PRINTERS.get(model)
    if not cfg or nozzle in ("", "0.4"):
        return cfg
    n = cfg.get("nozzles", {}).get(nozzle)
    return dict(cfg, **n) if n else None
BEDS = ["Cool Plate", "Engineering Plate", "High Temp Plate", "Textured PEI Plate", "Supertack Plate"]

jobs = {}
jobs_lock = threading.Lock()


def log(msg):
    print("slicer: " + msg, file=sys.stderr, flush=True)


def http_json(method, url, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw.decode(errors="replace")[:500]}


def trays(ams):
    out = []
    for unit in ((ams or {}).get("ams") or {}).get("ams") or []:
        for t in unit.get("tray") or []:
            if not t.get("tray_type"):
                continue  # empty slot
            out.append({"ams": str(unit.get("id", "0")), "id": str(t.get("id")), "type": t.get("tray_type"),
                        "idx": t.get("tray_info_idx") or "", "name": t.get("tray_sub_brands") or t.get("tray_type"),
                        "colour": "#" + (t.get("tray_color") or "C8C8C8FF")[:6]})
    return out


def printers():
    status, data = http_json("GET", SENDER + "/api/printers", timeout=10)
    if status != 200 or not isinstance(data, list):
        raise RuntimeError("Bridge answered %s: %s" % (status, data))
    out = []
    for p in data:
        model = p.get("dev_product_name") or p.get("dev_model_name") or ""
        cfg = PRINTERS.get(model)
        if not cfg:
            continue
        s, ams = http_json("GET", SENDER + "/api/ams/" + p["serial"], timeout=10)
        out.append({"serial": p["serial"], "name": p.get("name") or p["serial"], "model": model,
                    "bed": cfg["bed"], "qualities": list(cfg["quality"]),
                    "trays": trays(ams) if s == 200 else [], "online": bool(p.get("live"))})
    return out


def job_dir(jid):
    return os.path.join(JOBS, jid)


def save_job(job):
    path = os.path.join(job_dir(job["id"]), "job.json")
    write_json(path, job)
    os.chmod(path, 0o644)  # mkstemp makes 0600; the jobs folder is shared with tests that run as another user


def load_jobs():
    os.makedirs(JOBS, exist_ok=True)
    for jid in os.listdir(JOBS):
        try:
            with open(os.path.join(job_dir(jid), "job.json")) as f:
                job = json.load(f)
        except (OSError, ValueError):
            continue
        if job.get("state") in ("queued", "slicing"):  # a job of an older version that sliced on the server
            job.update(state="failed", error="This job was left by an older version. Slice again in the browser.")
        jobs[jid] = job


def prune():
    with jobs_lock:
        old = sorted(jobs.values(), key=lambda j: j["created"])[:-KEEP_JOBS]
        for j in old:
            jobs.pop(j["id"], None)
            shutil.rmtree(job_dir(j["id"]), ignore_errors=True)


def slice_info(path):
    """Print time, grams and filaments from the slice's Metadata/slice_info.config."""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("Metadata/slice_info.config"))
        names = z.namelist()
    plate = root.find("plate")
    meta = {m.get("key"): m.get("value") for m in plate.iter("metadata")}
    fils = [{k: f.get(k) for k in ("id", "type", "color", "used_g", "used_m")} for f in plate.iter("filament")]
    warn = [w.get("msg") for w in plate.iter("warning")]
    return {"seconds": int(float(meta.get("prediction") or 0)), "grams": float(meta.get("weight") or 0),
            "filaments": fils, "warnings": warn, "has_png": "Metadata/plate_1.png" in names,
            "objects": [o.get("name") for o in plate.iter("object")]}


def print_name(job):
    stem = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.splitext(job["filename"])[0])[:60] or "model"
    return "%s_%s_%s.gcode.3mf" % (stem, job["quality"], job["printer"]["model"].lower())


# --- HTTP handlers -------------------------------------------------------------------------------

TYPES = {".wasm": "application/wasm", ".mjs": "text/javascript", ".js": "text/javascript",
         ".json": "application/json", ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8"}


def get_engine(h, m, q):
    root = os.path.realpath(ENGINE)
    path = os.path.realpath(os.path.join(root, m.group(1)))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return h.send(404, {"error": "not found"})
    # Runtime folders are named after the engine release; version.json and presets change with the app.
    immutable = "/" in m.group(1) and not m.group(1).startswith("presets/")
    h.send_file(path, TYPES.get(os.path.splitext(path)[1], "application/octet-stream"),
                "public, max-age=31536000, immutable" if immutable else "no-cache")


def get_printers(h, m, q):
    try:
        h.send(200, {"printers": printers(), "beds": BEDS})
    except (OSError, RuntimeError) as e:
        h.send(502, {"error": "Cannot read printers from the bridge: %s" % e})


def read_body(h, f, length):
    left = length
    while left:
        chunk = h.rfile.read(min(left, 1 << 20))
        if not chunk:
            break
        f.write(chunk)
        left -= len(chunk)
    return length - left


def refuse(h, status, body):
    """Answer an error to an upload. The body is read first (up to 8 MB), or the client sees a reset connection, not the error."""
    left = min(content_length(h.headers), 8 << 20)
    while left > 0:
        left -= len(h.rfile.read(min(left, 1 << 20)) or b"\0" * left)
    h.send(status, body)


def chunk_args(h, q):
    """(offset, total) of an upload sent in parts, or None when the body is the whole file. A proxy in front of the app
    (HA ingress) can refuse bodies of about 1 MB, so the browser sends the file in parts of 512 KB."""
    if "total" not in q:
        return None
    try:
        offset, total = int((q.get("offset") or ["0"])[0]), int(q["total"][0])
    except ValueError:
        total = 0
    length = content_length(h.headers)
    if not 0 < total <= MAX_MODEL or offset < 0 or not 0 < length <= min(MAX_PART, total - offset):
        refuse(h, 413 if length > MAX_PART else 400, {"error": "Send the file in parts of at most %d bytes with a valid offset and total." % MAX_PART})
        return False
    return offset, total


def post_job(h, m, q):
    name = os.path.basename((q.get("name") or [""])[0])
    if not re.search(r"\.(stl|3mf|obj|step|stp)$", name, re.I):
        return h.send(400, {"error": "Upload an .stl, .3mf, .obj or .step file."})
    part = chunk_args(h, q)
    if part is False:
        return
    length = content_length(h.headers)
    total = part[1] if part else length
    if part and part[0]:
        return h.send(400, {"error": "The first part starts at offset 0."})
    if not 0 < length <= MAX_MODEL:
        return h.send(413, {"error": "The file must be 1 byte to 100 MB."})
    jid = secrets.token_hex(6)
    d = job_dir(jid)
    os.makedirs(d)
    model = os.path.join(d, re.sub(r"[^A-Za-z0-9._-]", "_", name))
    with open(model, "wb") as f:
        read_body(h, f, length)
    done = length == total
    job = {"id": jid, "filename": name, "model": model, "state": "uploaded" if done else "uploading", "created": time.time()}
    if not done:
        job["total"] = total
    with jobs_lock:
        jobs[jid] = job
    save_job(job)
    prune()
    h.send(200, dict(public(job), received=length) if part else public(job))


def post_model_part(h, m, q):
    """The next part of a model that POST /api/jobs started with `total`."""
    job = jobs.get(m.group(1))
    if not job:
        return h.send(404, {"error": "unknown job"})
    part = chunk_args(h, q)
    if part is False:
        return
    if not part or job.get("state") != "uploading" or part[1] != job.get("total"):
        return refuse(h, 400, {"error": "This job is not waiting for more parts of the model."})
    size = os.path.getsize(job["model"])
    if part[0] != size:
        return refuse(h, 409, {"error": "Expected offset %d." % size, "received": size})
    length = content_length(h.headers)
    with open(job["model"], "ab") as f:
        read_body(h, f, length)
    size = os.path.getsize(job["model"])
    if size == job["total"]:
        job.update(state="uploaded")
        job.pop("total", None)
    save_job(job)
    h.send(200, dict(public(job), received=size))


def public(job):
    return {k: v for k, v in job.items() if k not in ("model", "result")}


def get_job(h, m, q):
    job = jobs.get(m.group(1))
    if not job:
        return h.send(404, {"error": "unknown job"})
    h.send(200, public(job))


def pick(h, body):
    """The printer, quality, plate and tray a request names, or None after answering with the error."""
    try:
        printer = next(p for p in printers() if p["serial"] == body.get("serial"))
    except StopIteration:
        return h.send(400, {"error": "Unknown printer."})
    except (OSError, RuntimeError) as e:
        return h.send(502, {"error": str(e)})
    cfg = printer_cfg(printer["model"], body.get("nozzle") or "0.4")
    if not cfg:
        return h.send(400, {"error": "No profiles for a %s mm nozzle on this printer." % body["nozzle"]})
    quality, bed = body.get("quality"), body.get("bed") or printer["bed"]
    if quality not in cfg["quality"] or bed not in BEDS:
        return h.send(400, {"error": "Unknown quality or plate."})
    tray = tray_of(printer, body)
    if not tray:
        return h.send(400, {"error": "Pick a loaded AMS tray or a spool from the library."})
    return printer, quality, bed, tray


def tray_of(printer, body):
    """A loaded AMS tray (tray=ams:id), else a library spool named by type/brand/name/colour (not loaded, so no ams/id)."""
    if body.get("tray"):
        return next((t for t in printer["trays"] if t["ams"] + ":" + t["id"] == body["tray"]), None)
    typ = str(body.get("type") or "").strip()
    if not typ:
        return None
    colour = re.sub(r"[^0-9A-Fa-f]", "", str(body.get("colour") or ""))[:6]
    brand, name = str(body.get("brand") or "").strip(), str(body.get("name") or "").strip()
    return {"ams": None, "id": None, "type": typ, "idx": "", "brand": brand, "name": name or typ,
            "colour": "#" + (colour if len(colour) == 6 else "C8C8C8"), "spool_id": str(body.get("spool") or "") or None}


def presets_of(model, nozzle="0.4"):
    with open(os.path.join(ENGINE, "presets", model + ("" if nozzle in ("", "0.4") else "-" + nozzle) + ".json")) as f:
        return json.load(f)


def want(tray):
    """First word of the brand ("Bambu Lab" is "Bambu" in preset names) and the name of a library spool, for the preset
    match; empty for an AMS tray (it has a filament id)."""
    brand = (tray.get("brand") or "").split()[:1]
    return " ".join(brand + [tray.get("name") or ""]).strip() if tray.get("ams") is None else ""


def choose_filament(filaments, tray):
    """The tray's filament id, else a preset named like brand and name (library spools),
    else Generic of its type; fewest printers first."""
    words = want(tray).lower().split()
    def named(f):
        n = f["name"].lower().split(" @")[0]
        return f["type"] == tray["type"] and bool(words) and all(w in n for w in words)
    def rank(f):
        return (0 if tray["idx"] and f["id"] == tray["idx"] else 1 if named(f)
                else 2 if f["name"].startswith("Generic ") and f["type"] == tray["type"] else 9)
    ok = sorted((f for f in filaments if rank(f) < 9), key=lambda f: (rank(f), f["n"], f["name"]))
    return ok[0] if ok else None


def get_filament(h, m, q):
    """The filament preset (name) the browser slices a tray with."""
    arg = {k: v[0] for k, v in q.items()}
    try:
        printer = next(p for p in printers() if p["serial"] == arg.get("serial"))
        tray = tray_of(printer, arg)
        if not tray:
            raise StopIteration
        nozzle = arg.get("nozzle") or "0.4"
        if not printer_cfg(printer["model"], nozzle):
            return h.send(400, {"error": "No profiles for a %s mm nozzle on this printer." % nozzle})
        f = choose_filament(presets_of(printer["model"], nozzle)["filaments"], tray)
    except StopIteration:
        return h.send(400, {"error": "Pick a printer and a loaded AMS tray or a library spool."})
    except (OSError, RuntimeError) as e:
        return h.send(502, {"error": str(e)})
    if not f:
        return h.send(400, {"error": "No filament preset for %s (%s)." % (tray["name"], tray["type"])})
    # A preset that wants a hardened nozzle (required_nozzle_HRC above the plain steel value) is refused for a stainless one.
    if arg.get("nozzle_type") == "stainless_steel" and max(map(int, f["config"].get("required_nozzle_HRC") or [0])) > 3:
        return h.send(400, {"error": "%s needs a hardened steel nozzle." % f["name"].split(" @")[0]})
    h.send(200, {"filament": f["name"]})


def post_gcode(h, m, q):
    """G-code the browser sliced: packed as a .gcode.3mf and stored like a slice made here."""
    job = jobs.get(m.group(1))
    if not job:
        return h.send(404, {"error": "unknown job"})
    arg = {k: v[0] for k, v in q.items()}
    length = content_length(h.headers)
    part = chunk_args(h, q)
    if part is False:
        return
    if part:
        buf = os.path.join(job_dir(job["id"]), "gcode.part")
        size = os.path.getsize(buf) if os.path.isfile(buf) else 0
        if part[0] == 0:
            size = 0
        if part[0] != size:
            return refuse(h, 409, {"error": "Expected offset %d." % size, "received": size})
        with open(buf, "wb" if part[0] == 0 else "ab") as f:
            read_body(h, f, length)
        if os.path.getsize(buf) != part[1]:
            return h.send(200, {"received": os.path.getsize(buf), "total": part[1]})
        with open(buf, "rb") as f:
            gcode = f.read()
        os.remove(buf)
        length = len(gcode)
    else:
        if not 0 < length <= MAX_MODEL:
            return h.send(413, {"error": "The G-code must be 1 byte to 100 MB."})
        gcode = h.rfile.read(length)
        if len(gcode) != length:
            return h.send(400, {"error": "The G-code upload was cut short."})
    picked = pick(h, arg)
    if not picked:
        return
    printer, quality, bed, tray = picked
    nozzle = arg.get("nozzle") or "0.4"
    cfg = printer_cfg(printer["model"], nozzle)
    try:
        presets = presets_of(printer["model"], nozzle)
        filament = next(x for x in presets["filaments"] if x["name"] == arg.get("filament"))
        seconds, grams, metres = int(float(arg["seconds"])), float(arg["grams"]), float(arg.get("metres") or 0)
    except (OSError, StopIteration, KeyError, ValueError):
        return h.send(400, {"error": "Unknown filament preset or missing print statistics."})
    out = os.path.join(job_dir(job["id"]), "out")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    result = os.path.join(out, os.path.splitext(os.path.basename(job["model"]))[0] + ".gcode.3mf")
    pack.pack(result, gcode, {"machine": presets["machine"], "process": presets["process"][quality]},
              filament, bed, cfg["model_id"], tray["colour"], seconds, grams, metres, job["filename"], nozzle or "0.4")
    job.update(state="done", result=result, info=slice_info(result), quality=quality, nozzle=nozzle, error=None, sent=None,
               printer={k: printer[k] for k in ("serial", "name", "model")},
               settings={"printer": cfg["preset"], "process": cfg["quality"][quality], "filament": filament["name"],
                         "bed": bed, "tray": tray},
               log="Sliced in the browser (%s engine, %s s)." % (arg.get("engine", "?"), arg.get("slice_s", "?")),
               finished=time.time())
    save_job(job)
    h.send(200, public(job))


def get_file(h, m, q):
    job = jobs.get(m.group(1))
    if not job or job.get("state") != "done":
        return h.send(404, {"error": "no slice"})
    member = {"plate.png": "Metadata/plate_1.png", "plate.gcode": "Metadata/plate_1.gcode"}[m.group(2)]
    with zipfile.ZipFile(job["result"]) as z:
        if member not in z.namelist():
            return h.send(404, {"error": "not in the slice"})
        data = z.read(member)
    h.send(200, data, "image/png" if member.endswith("png") else "text/plain; charset=utf-8")


def get_model(h, m, q):
    """The uploaded model, so New print can slice it again (Reslice)."""
    job = jobs.get(m.group(1))
    if not job or not os.path.isfile(job.get("model") or ""):
        return h.send(404, {"error": "no model"})
    with open(job["model"], "rb") as f:
        h.send(200, f.read(), "application/octet-stream")


def post_print(h, m, q):
    job = jobs.get(m.group(1))
    if not job or job.get("state") != "done":
        return h.send(404, {"error": "no slice"})
    body = h.json()
    if body.get("confirm") is not True:
        return h.send(400, {"error": "Confirm the print first."})
    os.makedirs(SHARE, exist_ok=True)
    dest = os.path.join(SHARE, print_name(job).replace(".gcode.3mf", "_%s.gcode.3mf" % secrets.token_hex(3)))
    shutil.copyfile(job["result"], dest)
    call = {"serial": job["printer"]["serial"], "file": dest, "use_ams": True, "bed_leveling": True,
            "timelapse": bool(body.get("timelapse")), "force": bool(body.get("force"))}
    status = None
    try:
        if PRINT:  # the app sends the file itself
            try:
                status, answer = 200, PRINT(call)
            except Exception as e:  # noqa: BLE001 - the refusal text is what the person needs
                status, answer = getattr(e, "status", 400 if isinstance(e, ValueError) else 502), {"error": str(e)}
        else:
            token = env("SUPERVISOR_TOKEN", "")
            status, answer = http_json("POST", CORE + "/services/druckwerk/print_file?return_response", call,
                                       {"Authorization": "Bearer " + token}, timeout=360)
    finally:
        # The printer's picture (integration, preview) reads the sent copy while it prints, so the copy of a sent
        # print stays until this slice is sent again; a refused send leaves none.
        old = (job.get("sent") or {}).get("file")
        for path in ([dest] if status != 200 else []) + ([old] if status == 200 and old and old != dest else []):
            try:
                os.remove(path)
            except OSError:
                pass
    job["sent"] = {"at": time.time(), "file": dest, "status": status, "answer": answer}
    save_job(job)
    log("print %s -> %s: %s" % (os.path.basename(dest), job["printer"]["serial"], status))
    if status != 200:
        msg = answer.get("message") or answer.get("error") if isinstance(answer, dict) else answer
        if PRINT:
            return h.send(status, {"error": msg})
        return h.send(502, {"error": "Home Assistant refused the print (%s): %s" % (status, msg)})
    h.send(200, {"answer": answer, "file": os.path.basename(dest)})


ROUTES = [
    ("GET", r"/engine/(.+)", get_engine),
    ("GET", r"/api/engine/filament", get_filament),
    ("GET", r"/api/printers", get_printers),
    ("POST", r"/api/jobs", post_job),
    ("GET", r"/api/jobs/" + JOB_ID, get_job),
    ("POST", r"/api/jobs/" + JOB_ID + r"/gcode", post_gcode),
    ("GET", r"/api/jobs/" + JOB_ID + r"/(plate\.png|plate\.gcode)", get_file),
    ("GET", r"/api/jobs/" + JOB_ID + r"/model", get_model),
    ("POST", r"/api/jobs/" + JOB_ID + r"/model", post_model_part),
    ("POST", r"/api/jobs/" + JOB_ID + r"/print", post_print),
]


def isolation_headers(h):
    """Cross-origin isolation gives the multi-thread engine its SharedArrayBuffer. COOP+COEP isolate a top-level tab;
    Document-Isolation-Policy (Chrome 137+) isolates a frame, such as the Home Assistant sidebar."""
    h.send_header("Cross-Origin-Opener-Policy", "same-origin")
    h.send_header("Cross-Origin-Embedder-Policy", "require-corp")
    h.send_header("Document-Isolation-Policy", "isolate-and-require-corp")
    h.send_header("Cross-Origin-Resource-Policy", "same-origin")


class Handler(BaseHTTPRequestHandler):
    timeout = 120  # a client that stops sending must not hold a thread
    def head(self, status, ctype, length, cache, etag=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        if etag:
            self.send_header("ETag", etag)
        isolation_headers(self)
        self.end_headers()

    def send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            self.head(status, ctype, len(data), "no-store")
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):  # the client gave up before the answer
            self.close_connection = True
            log("client went away: %s %s" % (self.command, urlsplit(self.path).path))

    def send_file(self, path, ctype, cache):
        with open(path, "rb") as f:
            self.head(200, ctype, os.fstat(f.fileno()).st_size, cache)
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def require_json(self):
        # A cross-site form can post text/plain without a preflight; only a script of our own origin sets this type.
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise ValueError("Content-Type must be application/json.")

    def json(self):
        length = content_length(self.headers)
        if length:  # a call without a body (the integration's) has no type
            self.require_json()
        if length > 8192:
            raise ValueError("Body too large.")
        body = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("Body must be a JSON object.")
        return body

    def dispatch(self, method):
        u = urlsplit(self.path)
        for meth, regex, fn in ROUTES:
            match = meth == method and re.fullmatch(regex, u.path)
            if match:
                try:
                    return fn(self, match, parse_qs(u.query))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
        self.send(404, {"error": "not found"})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def log_message(self, fmt, *args):
        pass


def main():
    load_jobs()
    server = ThreadingHTTPServer((env("SLICER_HOST", "0.0.0.0"), int(env("SLICER_PORT", "8099"))), Handler)
    server.daemon_threads = True
    log("listening on port %d" % server.server_address[1])
    server.serve_forever()


if __name__ == "__main__":
    main()
