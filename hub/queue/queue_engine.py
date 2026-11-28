"""Multi-printer print queue. Home Assistant I/O is injected so tests can use fakes.

get_state(entity_id) -> str | None          fresh state of the vendor integration's sensor
get_start_state(entity_id) -> str | None (optional)   the same for a start: None when the status is too old (default: get_state)
call_service(domain, service, data) -> any  raises on refusal (pausing and resuming automations)
send_print(data) -> any                     sends one file to a printer; raises on refusal, the message is kept on the job
get_trays(serial) -> [{"type": "PLA", "colour": "#RRGGBB"}]   loaded AMS trays
get_nozzle(serial) -> "0.4" | None (optional)   installed nozzle diameter in mm; None when unknown, which never blocks a start
dry (optional): start(serial, {"ams_id", "temp", "hours", "filament"}) and stop(serial, ams_id) raise on refusal;
    state(serial, ams_id) -> {"active": bool, "left_min": int} | None (None: unknown)
"""
import logging
import re
import secrets
import threading
import time

from queue_registry import read_json, write_json
import os

log = logging.getLogger(__name__)

HISTORY = 200
STARTABLE = {"idle", "finish", "failed"}  # print status values that allow a new job
NOT_A_TRAY = {"none", "unknown", "unavailable", ""}
START_TIMEOUT = 600
START_STATUS_AGE = 60  # seconds a print start may rely on a printer status
SEND_LOST = "send timed out; check the printer before you retry"  # the file may have reached the printer
# Send-time print options with the defaults of the bridge before they existed. TRI options also take "auto".
OPTIONS = {"timelapse": False, "bed_leveling": True, "use_ams": True, "flow_cali": True, "vibration_cali": True,
           "layer_inspect": False, "nozzle_offset_cali": False, "bed_type": "auto", "ext_change_assist": False}
TRI = ("bed_leveling", "flow_cali", "nozzle_offset_cali")
BED_TYPES = ("auto", "cool_plate", "eng_plate", "hot_plate", "textured_plate", "supertack_plate", "3d_effect_plate")
NAME_MAX = 80
# Drying limits: the dryers of AMS 2 Pro (45 to 65) and AMS HT (45 to 85); the bridge checks the exact unit.
DRY_TEMP, DRY_HOURS = (45, 85), (1, 72)
DRY_GRACE = 120  # seconds after the start in which the AMS may not report drying yet
DRY_SLACK = 600  # seconds past the set time before a job whose state is unknown counts as done
WAITING = ("queued", "pending_approval")  # not yet sent to a printer
# "sending": reserved for a printer while the file goes out, with no lock held; "starting": sent, the printer has not begun.
BUSY = ("sending", "starting", "printing", "drying")


class QueueError(Exception):
    """A request the queue refuses; the text is shown to the person."""


class NotFound(QueueError):
    pass


def _colour(c):
    return re.sub(r"[^0-9a-f]", "", str(c or "").lower())[:6]


def _name(name, default):
    if name is None or (isinstance(name, str) and not name.strip()):
        return default
    if not isinstance(name, str) or len(name.strip()) > NAME_MAX:
        raise ValueError("name must be text of at most %d characters" % NAME_MAX)
    return name.strip()


def _option(k, v):
    if k not in OPTIONS:
        raise ValueError("bad option: " + str(k))
    ok = (v is True or v is False or v == "auto") if k in TRI else \
        v in BED_TYPES if k == "bed_type" else isinstance(v, bool)
    if not ok:
        raise ValueError("bad option: %s (%r)" % (k, v))
    return v


def _tray_ok(v):
    # Tray "0" is a real slot; only these words mean no filament.
    return v is not None and str(v).strip().lower() not in NOT_A_TRAY


class Queue:
    def __init__(self, registry, data_dir, get_state, call_service, get_trays=None,
                 clock=time.time, start_timeout=START_TIMEOUT, job_allowed=None, dry=None, send_print=None, get_nozzle=None, get_requests=None,
                 get_start_state=None):
        self.registry = registry
        self.path = os.path.join(data_dir, "queue.json")
        self.get_state, self.call_service = get_state, call_service
        self.get_start_state = get_start_state or get_state
        self.send_print = send_print  # send_print(data) sends one file; raises with the refusal text
        self.get_trays = get_trays or (lambda serial: [])
        self.clock, self.start_timeout = clock, start_timeout
        # job_allowed(job, serial) lets the access layer veto a job at start time (grant lapsed).
        self.job_allowed = job_allowed
        self.dry = dry
        self.get_nozzle = get_nozzle or (lambda serial: None)
        # get_requests(job_id, fresh) -> open change requests [{"wait": "0.6 mm nozzle", ...}]; a job with one is blocked.
        self.get_requests = get_requests or (lambda job_id, fresh=False: [])
        self.lock = threading.RLock()
        self.jobs, self.history = [], []
        # changing_filament lasts ~1 s, shorter than a poll; observe() latches it per serial.
        self.seen_change = {}
        os.makedirs(data_dir, exist_ok=True)
        saved = read_json(self.path, {})
        self.jobs, self.history = saved.get("jobs", []), saved.get("history", [])
        for j in list(self.jobs):  # the hub stopped during a send: the print may or may not have begun
            if j.get("state") == "sending":
                self._finish(j, "failed", "the app restarted while the file was sent; check the printer before a retry")
        for j in self.jobs + self.history:  # jobs saved before kinds and names existed
            j.setdefault("kind", "print")
            j.setdefault("name", str(j.get("file", ""))[:-len(".gcode.3mf")] if str(j.get("file", "")).endswith(".gcode.3mf") else j.get("file", ""))

    # -- storage --
    def _save(self):
        del self.history[:-HISTORY]
        write_json(self.path, {"jobs": self.jobs, "history": self.history})

    def _find(self, job_id):
        for j in self.jobs:
            if j["id"] == job_id:
                return j
        raise NotFound("no such queued job")

    def _finish(self, job, state, error=None):
        job.update(state=state, error=error, finished_at=self.clock())
        self.jobs.remove(job)
        self.history.append(job)

    # -- jobs --
    def add(self, file, source="", target="any", model="", filaments=None, options=None,
            owner_user_id=None, owner_name="", pending=False, name=None):
        if not str(file or "").endswith(".gcode.3mf") or "/" in file or ".." in file:
            raise ValueError("file must be a .gcode.3mf name in the share")
        target = self._check_target(target, model)
        opts = dict(OPTIONS)
        for k, v in (options or {}).items():
            opts[k] = _option(k, v)
        name = _name(name, file[:-len(".gcode.3mf")])
        fil = []
        for f in filaments or []:
            if not f.get("type"):
                raise ValueError("each filament needs a type")
            fil.append({"type": str(f["type"]), "colour": f.get("colour") or ""})
        job = {"id": secrets.token_hex(6), "kind": "print", "name": name, "file": file, "source": source or file,
               "target": target, "model": model if target == "any" else "",
               "filaments": fil, "options": opts, "error": None,
               "state": "pending_approval" if pending else "queued",
               "owner_user_id": owner_user_id, "owner_name": owner_name,
               "created_at": self.clock()}
        with self.lock:
            self.jobs.append(job)
            self._save()
        return job

    def add_dry(self, target, ams_id, temp, hours, filament="", owner_user_id=None, owner_name="", pending=False, name=None):
        """A job that dries one AMS unit; it is queued, approved and started like a print job."""
        if not self.registry.get(target):
            raise ValueError("unknown printer")
        if isinstance(ams_id, bool) or not isinstance(ams_id, int) or not (0 <= ams_id <= 3 or 128 <= ams_id <= 135):
            raise ValueError("ams_id must be 0 to 3, or 128 to 135 for an AMS HT")
        for label, v, (lo, hi) in (("temp", temp, DRY_TEMP), ("hours", hours, DRY_HOURS)):
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError("%s must be a whole number from %d to %d" % (label, lo, hi))
        if not isinstance(filament, str) or len(filament) > 40:
            raise ValueError("filament must be text of at most 40 characters")
        unit = "AMS %d" % (ams_id + 1) if ams_id < 128 else "AMS HT"
        label = "Dry %s%s" % (unit, " (%s)" % filament if filament else "")
        job = {"id": secrets.token_hex(6), "kind": "dry", "name": _name(name, label), "file": "", "source": label,
               "target": target, "model": "", "filaments": [], "options": {}, "error": None,
               "dry": {"ams_id": ams_id, "temp": temp, "hours": hours, "filament": filament},
               "state": "pending_approval" if pending else "queued",
               "owner_user_id": owner_user_id, "owner_name": owner_name, "created_at": self.clock()}
        with self.lock:
            self.jobs.append(job)
            self._save()
        return job

    def _check_target(self, target, model):
        if target == "any":
            if not str(model or "").strip():
                raise ValueError("model is required for target any")
        elif not self.registry.get(target):
            raise ValueError("unknown printer")
        return target

    def reorder(self, job_id, position, owner=None, scope=None):
        """position is 0-based among the waiting jobs, or among the owner's when owner is given.
        The jobs are permuted within the slots they already hold, so nobody else moves.
        scope (a serial) narrows the pool to waiting jobs whose target or printer is that serial."""
        with self.lock:
            job = self._find(job_id)
            if job["state"] not in WAITING:
                raise QueueError("only waiting jobs can be reordered")
            slots = [i for i, j in enumerate(self.jobs)
                     if j["state"] in WAITING and (owner is None or j.get("owner_user_id") == owner)
                     and (scope is None or scope in (j["target"], j.get("printer")))]
            pool = [self.jobs[i] for i in slots]
            if job not in pool:
                raise QueueError("job is not in this reorder scope")
            pool.remove(job)
            pool.insert(max(0, min(int(position), len(pool))), job)
            for i, j in zip(slots, pool):
                self.jobs[i] = j
            self._save()
            return job

    def move(self, job_id, target, model=""):
        with self.lock:
            job = self._find(job_id)
            if job["state"] not in WAITING:
                raise QueueError("only waiting jobs can be moved")
            if job.get("kind") == "dry":
                raise ValueError("a drying job belongs to one printer")
            job["target"] = self._check_target(target, model or job["model"])
            job["model"] = (model or job["model"]) if target == "any" else ""
            self._save()
            return job

    def edit(self, job_id, name=None, options=None):
        """Change the name or print options of a job that has not started."""
        with self.lock:
            job = self._find(job_id)
            if job["state"] not in WAITING:
                raise QueueError("only waiting jobs can be edited")
            if job.get("kind") == "dry":
                raise ValueError("a drying job has no name or options to edit")
            if name is None and options is None:
                raise ValueError("send a name or options")
            new_name = _name(name, job["name"]) if name is not None else job["name"]
            if options is not None and not isinstance(options, dict):
                raise ValueError("options must be an object")
            new_opts = dict(job["options"], **{k: _option(k, v) for k, v in (options or {}).items()})
            job.update(name=new_name, options=new_opts)
            self._save()
            return job

    def cancel(self, job_id, force=False):
        """force (the access layer allows it to admins only) removes a printing job the printer never reports as ended."""
        with self.lock:
            job = self._find(job_id)
            if job["state"] == "drying":
                raise QueueError("this job is drying; stop it first")
            if force and job["state"] == "printing":
                self._finish(job, "failed", "removed by admin")
                self._save()
                return job
            if job["state"] not in WAITING:
                raise QueueError("a started job runs on the printer; stop it there")
            self._finish(job, "cancelled")
            self._save()
            return job

    def retry(self, job_id):
        with self.lock:
            job = next((j for j in self.history if j["id"] == job_id), None)
            if not job:
                raise NotFound("no such finished job")
            if job["state"] not in ("failed", "cancelled"):
                raise QueueError("only failed or cancelled jobs can be retried")
            self.history.remove(job)
            for k in ("finished_at", "started_at"):
                job.pop(k, None)
            job.update(state="queued", error=None, printer=None)
            self.jobs.append(job)
            self._save()
            return job

    def approve(self, job_id):
        with self.lock:
            job = self._find(job_id)
            if job["state"] != "pending_approval":
                raise QueueError("job is not waiting for approval")
            job["state"] = "queued"
            self._save()
            return job

    def reject(self, job_id):
        with self.lock:
            job = self._find(job_id)
            if job["state"] != "pending_approval":
                raise QueueError("job is not waiting for approval")
            self._finish(job, "cancelled", "rejected")
            self._save()
            return job

    def skip(self, job_id):
        """Send a waiting job to the end of the queue."""
        with self.lock:
            job = self._find(job_id)
            if job["state"] not in WAITING:
                raise QueueError("only waiting jobs can be skipped")
            self.jobs.remove(job)
            self.jobs.append(job)
            self._save()
            return job

    # -- matching --
    def _filaments_fit(self, job, serial):
        # Each wanted filament needs its own tray of the same type and colour.
        free = list(self.get_trays(serial) or [])
        for want in job["filaments"]:
            hit = next((t for t in free if t.get("type", "").lower() == want["type"].lower()
                        and (not want["colour"] or _colour(t.get("colour")) == _colour(want["colour"]))), None)
            if not hit:
                return False
            free.remove(hit)
        return True

    def _fits(self, job, serial, printer):
        if job["target"] == serial:
            return True
        return (job["target"] == "any" and job["model"].lower() == printer["model"].lower()
                and self._filaments_fit(job, serial))

    def blocked(self, job, fresh=False):
        """Why a waiting job cannot start yet (an open request for a nozzle or spool change), else None."""
        reqs = self.get_requests(job["id"], fresh)
        return "This job is waiting for %s." % " and ".join(r["wait"] for r in reqs) if reqs else None

    def next_for(self, serial, fresh=False):
        printer = self.registry.get(serial)
        if not printer:
            return None
        for job in self.jobs:
            # pending_approval is skipped here: only an admin's approval makes a job startable.
            if job["state"] == "queued" and self._fits(job, serial, printer) \
                    and (not self.job_allowed or self.job_allowed(job, serial)) and not self.blocked(job, fresh):
                return job
        return None

    def _busy(self, serial):
        return next((j for j in self.jobs if j["state"] in BUSY and j.get("printer") == serial), None)

    # -- start --
    def start(self, serial, plate_clear=False, job_id=None, confirmed=False):
        # Only a person starts a job: a print after confirming the plate is clear, a drying after confirming the start.
        # The lock covers the choice and the reservation only; the send itself runs without it.
        printer = self.registry.get(serial)
        if not printer:
            raise NotFound("unknown printer")
        status = self.get_start_state(self.registry.entities(serial)["print_status"])
        if status is None:  # None is also what an unreadable sensor gives; the person can only wait or look
            raise QueueError("the status of the printer is older than %d s or unknown; try again in a moment" % START_STATUS_AGE)
        with self.lock:
            if self._busy(serial):
                raise QueueError("this printer already has a job starting, printing or drying")
            if status not in STARTABLE:
                raise QueueError("printer is not ready (status: %s)" % status)
            if job_id:
                # Explicit pick by an admin: skips the queue order and the owner's grant check.
                job = self._find(job_id)
                if job["state"] != "queued" or not self._fits(job, serial, printer):
                    raise QueueError("that job is not queued or does not fit this printer")
            else:
                job = self.next_for(serial, fresh=True)
                if not job:  # say why when the only fitting jobs wait for a change
                    whys = (self.blocked(j, True) for j in self.jobs if j["state"] == "queued" and self._fits(j, serial, printer))
                    raise QueueError(next((w for w in whys if w), "no queued job fits this printer"))
            why = self.blocked(job, True)
            if why:
                raise QueueError(why)
            dry = job.get("kind") == "dry"
            if dry and confirmed is not True:
                raise QueueError("confirm the start of the drying")
            if not dry:
                self._check_nozzle(job, serial)
            if not dry and plate_clear is not True:
                raise QueueError("confirm that the plate is clear")
            job.update(state="sending", printer=serial, started_at=self.clock())
            self.seen_change[serial] = False
            self._save()
        try:
            err, unsure = self._send_dry(serial, job) if dry else self._send(printer, serial, job)
        except BaseException as e:  # a bad request: nothing was sent
            self._unreserve(job, str(e))
            raise
        if unsure:  # a timeout: the file may be on the printer, a second send could print twice
            with self.lock:
                self._finish(job, "failed", SEND_LOST)
                self._save()
            raise QueueError(SEND_LOST)
        if err:
            self._unreserve(job, err)  # stays first in the queue
            raise QueueError(err)
        with self.lock:
            job.update(state="drying" if dry else "starting", error=None, started_at=self.clock())
            self._save()
            return job

    def _unreserve(self, job, error):
        with self.lock:
            job.update(state="queued", error=error)
            job.pop("printer", None)
            job.pop("started_at", None)
            self._save()

    def _check_nozzle(self, job, serial):
        need, have = job.get("nozzle"), self.get_nozzle(serial)
        if need and have and float(need) != float(have):
            raise QueueError("This job needs a %s mm nozzle; the printer has %s mm." % (need, have))

    def _send_dry(self, serial, job):
        if not self.dry:
            return "drying is not available", False
        try:
            self.dry.start(serial, dict(job["dry"]))
            return None, False
        except ValueError:  # the bridge refused the values: a bad request, not a busy printer
            raise
        except Exception as e:  # the bridge's refusal text is the useful part
            return str(e) or e.__class__.__name__, False

    def stop_dry(self, job_id):
        """Stop the AMS and end the job; if the bridge cannot stop it, the job stays so nobody starts a print on a hot AMS."""
        with self.lock:
            job = self._find(job_id)
            if job.get("kind") != "dry" or job["state"] != "drying":
                raise QueueError("this job is not drying")
            try:
                self.dry.stop(job["printer"], job["dry"]["ams_id"])
            except Exception as e:
                raise QueueError(str(e) or e.__class__.__name__)
            self._finish(job, "cancelled", "stopped")
            self._save()
            return job

    def _send(self, printer, serial, job):
        """Returns (error text or None, unsure). unsure: the send ended in a timeout. Paused entities come back either way."""
        paused = [e for e in printer["pause_entities"] if self.get_state(e) == "on"]
        err, unsure = None, False
        try:
            for e in paused:
                self.call_service("homeassistant", "turn_off", {"entity_id": e})
            o = job["options"]
            # force: the bridge status can be hours old; the fresh sensor was checked above.
            data = {"serial": serial, "file": job["file"], "timelapse": o["timelapse"],
                    "bed_leveling": o["bed_leveling"], "use_ams": o["use_ams"], "force": True}
            # Advanced options go along only when they differ from the default, as old jobs and callers expect.
            for k in OPTIONS:
                if k not in data and o.get(k, OPTIONS[k]) != OPTIONS[k]:
                    data[k] = o[k]
            self.send_print(data)
        except Exception as e:  # the guard's refusal text is the useful part
            err, unsure = str(e) or e.__class__.__name__, bool(getattr(e, "unsure", False))
        finally:
            stuck = []
            for e in paused:
                try:
                    self.call_service("homeassistant", "turn_on", {"entity_id": e})
                except Exception as x:
                    log.warning("could not switch %s on again: %s", e, str(x) or x.__class__.__name__)
                    stuck.append(e)
        if err and stuck:  # after a sent file the job runs on; only a refusal gets the hint
            err += " (still off: %s)" % ", ".join(stuck)
        return err, unsure

    # -- progress --
    def observe(self, serial, stage):
        """Feed stage changes from state events so a short changing_filament is not missed."""
        j = self._busy(serial)
        if stage == "changing_filament" and j and j["state"] in ("sending", "starting"):
            self.seen_change[serial] = True

    def tick(self, serial):
        # Read Home Assistant and the bridge first, then apply under the lock: a slow answer must not block the queue.
        with self.lock:
            job = self._busy(serial)
            if not job or job["state"] == "sending":
                return job
            job_id, state = job["id"], job["state"]
        ent = self.registry.entities(serial)
        dry_state = status = stage = tray = progress = None
        if state == "drying":
            try:
                dry_state = self.dry.state(serial, job["dry"]["ams_id"]) if self.dry else None
            except Exception:
                dry_state = None  # the bridge may be down; the set time still ends the job
        else:
            status, stage, tray = (self.get_state(ent[k]) for k in ("print_status", "current_stage", "active_tray"))
            if state == "starting" and _tray_ok(tray) and stage == "printing":
                progress = self.get_state(ent["print_progress"])
        with self.lock:
            job = self._busy(serial)
            if not job or job["id"] != job_id or job["state"] != state:  # changed while we read
                return job
            before = (job["state"], job.get("error"), job.get("finished_at"))
            if state == "drying":
                self._tick_drying(job, dry_state)
            elif state == "starting":
                self._tick_starting(job, serial, status, stage, tray, progress)
            else:
                self._tick_printing(job, status)
            if (job["state"], job.get("error"), job.get("finished_at")) != before:  # a running job would rewrite the file every tick
                self._save()
            return job

    def _tick_drying(self, job, state):
        d, age = job["dry"], self.clock() - job["started_at"]
        if state is None:
            if age > d["hours"] * 3600 + DRY_SLACK:
                self._finish(job, "done")
        elif age > DRY_GRACE and not state.get("active"):
            self._finish(job, "done")

    def _tick_starting(self, job, serial, status, stage, tray, progress):
        if stage == "changing_filament":
            self.seen_change[serial] = True
        loaded = _tray_ok(tray)
        started = loaded and (stage == "changing_filament" or self.seen_change.get(serial))
        if not started and loaded and stage == "printing":
            try:
                started = float(progress) > 0
            except (TypeError, ValueError):
                started = False
        # "running" alone proves nothing: a print once ran an hour with stage idle and no tray.
        if started:
            job["state"] = "printing"
        elif self.clock() - job["started_at"] > self.start_timeout:
            self._finish(job, "failed", "did not start within %d s (status %s, stage %s, tray %s)"
                         % (self.start_timeout, status, stage, tray))

    def _tick_printing(self, job, status):
        if status == "finish":
            self._finish(job, "done")
        elif status == "failed":
            self._finish(job, "failed", "printer reported a failed print")
        elif status == "idle":
            self._finish(job, "failed", "printer went idle before finishing")

    def snapshot(self):
        with self.lock:
            # Blocked state travels with the job, so the view needs no second request.
            return {"jobs": [dict(j, requests=self.get_requests(j["id"])) if j["state"] in WAITING else j for j in self.jobs],
                    "history": self.history[-HISTORY:][::-1],
                    "printers": self.registry.all()}
