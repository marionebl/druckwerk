"""Actions and LLM tools of the Druckwerk integration. The integration only forwards; this module owns what they do.

ACTIONS and TOOLS are tables, manifest() publishes them (contract: hub/API.md). Ops runs one call as the act-as user
through the app's own HTTP routes, so the access rules stay in one place.
"""
import hashlib
import json
import os
import time
from urllib.parse import quote

from entities import ENTITIES

API_VERSION = 1
SNAPSHOT_KEEP = 20  # snapshots kept per printer
QUEUE_TIMEOUT = 120  # the app copies the file into the share before it answers
MEDIA_SUBDIR = "druckwerk"
CONFIRM_TEXT = (
    "Starting a print needs plate_clear: true and confirmed_by_person: true. Show the person the sliced plate "
    "review and a current camera snapshot (druckwerk.camera_snapshot), ask them to confirm the plate is clear "
    "and the print may start, and call again only after an explicit yes."
)
BED_TYPES = ["auto", "cool_plate", "eng_plate", "hot_plate", "textured_plate", "supertack_plate", "3d_effect_plate"]
# Print options of the vendor slicer's dialog. A tri option takes on, off or "auto".
OPTIONS = {
    **{k: {"kind": "bool"} for k in ("timelapse", "use_ams", "vibration_cali", "layer_inspect", "ext_change_assist")},
    **{k: {"kind": "tri"} for k in ("bed_leveling", "flow_cali", "nozzle_offset_cali")},
    "bed_type": {"kind": "enum", "values": BED_TYPES},
}
TEXT, BOOL = {"text": {}}, {"boolean": {}}


class ActionError(Exception):
    """A failure with the HTTP status the route answers."""

    def __init__(self, status, text):
        super().__init__(text)
        self.status = status


def F(kind, title, description, **extra):
    """One field. kind, min, max and tool (the LLM text) stay in the app; the rest goes to the manifest."""
    return {"kind": kind, "title": title, "description": description, **extra}


ACTIONS = {
    "print_file": {
        "title": "Print file",
        "description": (
            "Sends a .gcode.3mf file to a printer as a cloud print through the add-on."
        ),
        "response": "optional",
        "serial_fields": ["device_id"],
        "fields": {
            "device_id": F(
                "string", "Printer",
                "Printer device, as registered by the printer integration.",
                selector={"device": {"filter": [{"manufacturer": "Bambu Lab"}]}}),
            "serial": F(
                "string", "Serial",
                "Printer serial number, used when no device is given.",
                example="0000TEST0000001", selector=TEXT),
            "file": F(
                "string", "File",
                "Path relative to /media/druckwerk, or an absolute path under /media or /share.",
                required=True, example="benchy.gcode.3mf", selector=TEXT),
            "timelapse": F(
                "bool", "Timelapse",
                "Record a timelapse.",
                default=False, selector=BOOL),
            "bed_leveling": F(
                "tri", "Bed leveling",
                "Level the bed before printing: on, off or auto (the printer decides).",
                default="on", selector={"select": {"options": ["on", "off", "auto"]}}),
            "use_ams": F(
                "bool", "Use AMS",
                "Use the AMS filament mapping.",
                default=True, selector=BOOL),
            "flow_cali": F(
                "tri", "Flow dynamics calibration",
                "Calibrate the flow before printing: on, off or auto. Left out, the bridge keeps its "
                "default (on).",
                advanced=True, selector={"select": {"options": ["on", "off", "auto"]}}),
            "vibration_cali": F(
                "bool", "Vibration calibration",
                "Run the vibration (resonance) calibration. Left out, the bridge keeps its default "
                "(on).",
                advanced=True, selector=BOOL),
            "layer_inspect": F(
                "bool", "First layer inspection",
                "Inspect the first layer with the printer camera. Left out: off.",
                advanced=True, selector=BOOL),
            "nozzle_offset_cali": F(
                "tri", "Nozzle offset calibration",
                "Calibrate the nozzle offset: on, off or auto. For printers with two nozzles. Left "
                "out: off.",
                advanced=True, selector={"select": {"options": ["off", "on", "auto"]}}),
            "bed_type": F(
                "enum", "Plate type",
                "The build plate in the printer. auto means the file decides. Left out: auto.",
                advanced=True, selector={"select": {"options": ["auto", "cool_plate", "eng_plate", "hot_plate", "textured_plate", "supertack_plate", "3d_effect_plate"]}}),
            "ext_change_assist": F(
                "bool", "External spool change assist",
                "Let the printer help when you change the external spool. Left out: off.",
                advanced=True, selector=BOOL),
            "force": F(
                "bool", "Force",
                "Send even if the printer is running or paused.",
                default=False, selector=BOOL),
        },
    },
    "get_printers": {
        "title": "Get printers",
        "description": (
            "Returns the printers you may use: status, progress, remaining time and end time, layer, "
            "temperatures, AMS trays (unit and slot, type, colour, remaining), HMS errors, the "
            "current job and the queue length. Read-only. The serial and the AMS trays feed the other"
            " actions."
        ),
        "response": "only",
        "serial_fields": ["printer"],
        "fields": {
            "printer": F(
                "string", "Printer",
                "Only this printer: serial number, printer name or device id. Empty returns all "
                "printers you may use.",
                tool="Only this printer: serial or name",
                example="Lab H2S", selector=TEXT),
        },
    },
    "get_queue": {
        "title": "Get queue",
        "description": (
            "Returns the print queue: waiting jobs per printer serial and for \"any\" (each with its "
            "0-based position, as reorder_job takes it), jobs that wait for approval, running jobs "
            "and optionally the latest finished jobs. Read-only."
        ),
        "response": "only",
        "serial_fields": [],
        "fields": {
            "history_limit": F(
                "int", "History limit",
                "How many finished jobs to include, newest first. 0 for none.",
                tool="Finished jobs to include, 0 to 50",
                default=0, selector={"number": {"min": 0, "max": 50, "mode": "box"}}, min=0, max=50),
        },
    },
    "add_to_queue": {
        "title": "Add to queue",
        "description": (
            "Adds a job to the print queue; it does not start a print. The file is a sliced "
            ".gcode.3mf that is already in the share. A person who needs approval creates a job "
            "that waits for an admin. Starting is a separate step that needs the person's confirmation."
        ),
        "response": "optional",
        "serial_fields": ["target"],
        "fields": {
            "file": F(
                "string", "File",
                "Name of a sliced .gcode.3mf in the share, or an absolute path under /media or /share"
                " to one.",
                tool="Sliced .gcode.3mf name in the share",
                required=True, example="benchy_better_h2s.gcode.3mf", selector=TEXT),
            "target": F(
                "string", "Target",
                "Printer serial number or name, or \"any\" for the first free printer of the model.",
                tool="Printer serial or name, or any",
                default="any", selector=TEXT),
            "model": F(
                "string", "Model",
                "Printer model for target \"any\", for example H2S. Needed when the printers have "
                "different models.",
                tool="Printer model for target any, e.g. H2S",
                example="H2S", selector=TEXT),
            "options": F(
                "options", "Options",
                "Print options: timelapse, use_ams, vibration_cali, layer_inspect, ext_change_assist "
                "as booleans; bed_leveling, flow_cali, nozzle_offset_cali as true, false or auto; "
                "bed_type as auto, cool_plate, eng_plate, hot_plate, textured_plate, supertack_plate "
                "or 3d_effect_plate.",
                tool=(
                    "Print options: timelapse, use_ams, vibration_cali, layer_inspect, "
                    "ext_change_assist as booleans; bed_leveling, flow_cali, nozzle_offset_cali as "
                    "true, false or auto; bed_type as auto, cool_plate, eng_plate, hot_plate, "
                    "textured_plate, supertack_plate or 3d_effect_plate"
                ),
                example={"timelapse": True, "bed_leveling": "auto", "bed_type": "textured_plate"}, selector={"object": {}}),
            "name": F(
                "name", "Name",
                "Name of the print, up to 80 characters. The queue and the live activity show it. "
                "Default: the file name without .gcode.3mf.",
                tool=(
                    "Name of the print, shown in the queue and the live activity (80 characters)"
                ),
                example="Benchy for Anna", selector=TEXT),
        },
    },
    "cancel_job": {
        "title": "Cancel job",
        "description": (
            "Cancels a waiting job. Users cancel their own jobs; printer admins cancel any job. A job"
            " that already started runs on the printer and cannot be cancelled here."
        ),
        "response": "optional",
        "serial_fields": [],
        "fields": {
            "job_id": F(
                "string", "Job",
                "Queue job id (12 hex characters) from get_queue.",
                tool="Queue job id from druckwerk_get_queue",
                required=True, example="a1b2c3d4e5f6", selector=TEXT),
        },
    },
    "reorder_job": {
        "title": "Reorder job",
        "description": (
            "Moves a waiting job to a position in the queue. Users reorder only within the slots of "
            "their own jobs; printer admins reorder any job."
        ),
        "response": "optional",
        "serial_fields": ["printer"],
        "fields": {
            "job_id": F(
                "string", "Job",
                "Queue job id (12 hex characters) from get_queue.",
                tool="Queue job id from druckwerk_get_queue",
                required=True, example="a1b2c3d4e5f6", selector=TEXT),
            "position": F(
                "int", "Position",
                "New 0-based position among the waiting jobs (of the printer, when printer is set).",
                tool="New 0-based position",
                required=True, example=0, selector={"number": {"min": 0, "max": 500, "mode": "box"}}, min=0),
            "printer": F(
                "string", "Printer scope",
                "Count positions among the waiting jobs of this printer only. Empty counts all "
                "waiting jobs.",
                tool="Count positions among this printer's waiting jobs",
                selector=TEXT),
        },
    },
    "move_job": {
        "title": "Move job",
        "description": (
            "Changes the target of a waiting job to another printer or to \"any\". Needs admin on the "
            "current and on the new printer."
        ),
        "response": "optional",
        "serial_fields": ["target"],
        "fields": {
            "job_id": F(
                "string", "Job",
                "Queue job id (12 hex characters) from get_queue.",
                tool="Queue job id from druckwerk_get_queue",
                required=True, example="a1b2c3d4e5f6", selector=TEXT),
            "target": F(
                "string", "Target",
                "New printer serial number or name, or \"any\".",
                tool="Printer serial or name, or any",
                required=True, example="any", selector=TEXT),
            "model": F(
                "string", "Model",
                "Printer model, needed when the target is \"any\" and the job has none.",
                tool="Printer model, needed for any",
                selector=TEXT),
        },
    },
    "skip_job": {
        "title": "Skip job",
        "description": (
            "Sends a waiting job to the end of the queue so the next one comes first. Printer admins "
            "only."
        ),
        "response": "optional",
        "serial_fields": [],
        "fields": {
            "job_id": F(
                "string", "Job",
                "Queue job id (12 hex characters) from get_queue.",
                tool="Queue job id from druckwerk_get_queue",
                required=True, example="a1b2c3d4e5f6", selector=TEXT),
        },
    },
    "approve_job": {
        "title": "Approve job",
        "description": (
            "Approves a job that waits for approval so it becomes queued. Printer admins only."
        ),
        "response": "optional",
        "serial_fields": [],
        "fields": {
            "job_id": F(
                "string", "Job",
                "Queue job id (12 hex characters) from get_queue.",
                tool="Queue job id from druckwerk_get_queue",
                required=True, example="a1b2c3d4e5f6", selector=TEXT),
        },
    },
    "reject_job": {
        "title": "Reject job",
        "description": (
            "Rejects a job that waits for approval; it is cancelled. Printer admins only."
        ),
        "response": "optional",
        "serial_fields": [],
        "fields": {
            "job_id": F(
                "string", "Job",
                "Queue job id (12 hex characters) from get_queue.",
                tool="Queue job id from druckwerk_get_queue",
                required=True, example="a1b2c3d4e5f6", selector=TEXT),
        },
    },
    "retry_job": {
        "title": "Retry job",
        "description": (
            "Puts a failed or cancelled job from the history back into the queue. Printer admins "
            "only."
        ),
        "response": "optional",
        "serial_fields": [],
        "fields": {
            "job_id": F(
                "string", "Job",
                "Queue job id (12 hex characters) from get_queue.",
                tool="Queue job id from druckwerk_get_queue",
                required=True, example="a1b2c3d4e5f6", selector=TEXT),
        },
    },
    "start_next": {
        "title": "Start next job",
        "description": (
            "Starts the next queued job on a printer. Printer admins only. SAFETY: this heats and "
            "moves a printer. Before calling, show the person the sliced plate review and a current "
            "camera snapshot (druckwerk.camera_snapshot), ask whether the plate is clear and the "
            "print may start, and call only after an explicit yes. Both plate_clear and "
            "confirmed_by_person must be true, otherwise the call is refused."
        ),
        "response": "optional",
        "serial_fields": ["printer"],
        "fields": {
            "printer": F(
                "string", "Printer",
                "Printer serial number, printer name or device id. get_printers lists them.",
                tool="Printer serial number or name",
                required=True, selector=TEXT),
            "plate_clear": F(
                "bool", "Plate clear",
                "Must be true: the build plate is empty and clean. Only set it after the person has "
                "looked at the current camera snapshot.",
                tool="True only after the person saw a current camera snapshot",
                default=False, selector=BOOL),
            "confirmed_by_person": F(
                "bool", "Confirmed by person",
                "Must be true: the person has seen the sliced plate review and a current camera "
                "snapshot and explicitly said the print may start. An agent must never set this on "
                "its own.",
                tool="True only after the person explicitly said to start",
                default=False, selector=BOOL),
        },
    },
    "start_job": {
        "title": "Start job",
        "description": (
            "Starts a chosen queued job on a printer, skipping the queue order. Printer admins only. "
            "SAFETY: this heats and moves a printer. Before calling, show the person the sliced plate"
            " review and a current camera snapshot (druckwerk.camera_snapshot), ask whether the plate"
            " is clear and the print may start, and call only after an explicit yes. Both plate_clear"
            " and confirmed_by_person must be true, otherwise the call is refused."
        ),
        "response": "optional",
        "serial_fields": ["printer"],
        "fields": {
            "printer": F(
                "string", "Printer",
                "Printer serial number, printer name or device id. get_printers lists them.",
                tool="Printer serial number or name",
                required=True, selector=TEXT),
            "job_id": F(
                "string", "Job",
                "Queue job id (12 hex characters) from get_queue.",
                tool="Queue job id from druckwerk_get_queue",
                required=True, example="a1b2c3d4e5f6", selector=TEXT),
            "plate_clear": F(
                "bool", "Plate clear",
                "Must be true: the build plate is empty and clean. Only set it after the person has "
                "looked at the current camera snapshot.",
                tool="True only after the person saw a current camera snapshot",
                default=False, selector=BOOL),
            "confirmed_by_person": F(
                "bool", "Confirmed by person",
                "Must be true: the person has seen the sliced plate review and a current camera "
                "snapshot and explicitly said the print may start. An agent must never set this on "
                "its own.",
                tool="True only after the person explicitly said to start",
                default=False, selector=BOOL),
        },
    },
    "camera_snapshot": {
        "title": "Camera snapshot",
        "description": (
            "Takes a current picture from the printer's camera and saves it as "
            "/media/druckwerk/<serial>-<time>.jpg; returns the path and a media source id. Takes 10 "
            "to 60 seconds when the camera tunnel is closed. Show it to the person before a start. "
            "Needs an active grant on the printer."
        ),
        "response": "only",
        "serial_fields": ["printer"],
        "fields": {
            "printer": F(
                "string", "Printer",
                "Printer serial number, printer name or device id. get_printers lists them.",
                tool="Printer serial number or name",
                required=True, selector=TEXT),
        },
    },
}

PROMPT = """Druckwerk manages 3D printers: printer status, the print queue and starting prints. Models are sliced in the browser, not by this app.
Rules:
- You act as the signed-in Home Assistant user. Home Assistant admins and printer owners are admins of their printers.
  Users may add jobs on printers they have an active grant for (some grants need an admin's approval), cancel their own jobs
  and reorder only within their own queue slots. Only admins skip, move, approve, reject, retry and start jobs. A refusal
  starting with "rule:" is final; tell the person, do not try another way round.
- This app has no slice tool. A person slices in the app's New print page, in the browser. You work with sliced
  .gcode.3mf files that are already in the share.
- Typical flow: druckwerk_get_printers (serial, AMS trays as unit:slot), druckwerk_add_to_queue with the sliced file
  name. Adding to the queue never starts a print.
- Never start a print on your own. Before druckwerk_start_next or druckwerk_start_job, show the person the sliced plate
  review and a current camera snapshot (druckwerk_camera_snapshot), ask whether the plate is clear and the print may
  start, and call only after an explicit yes, with plate_clear and confirmed_by_person both true. Without both the call is refused.
- Printers are named by serial number, printer name or device id. Job ids are 12 hex characters.
"""


def hints(read_only, destructive=False, idempotent=False, open_world=False):
    return {"read_only": read_only, "destructive": destructive, "idempotent": idempotent, "open_world": open_world}


READ, WRITE, CANCEL = hints(True, idempotent=True), hints(False), hints(False, destructive=True)
SAFETY = " Admins only. " + CONFIRM_TEXT
# tool name -> (action, description, annotations). The tool takes the fields of its action.
TOOLS = {
    "druckwerk_get_printers": ("get_printers", (
        "Status of the printers you may use: state, progress, remaining time, end time, layer, temperatures, "
        "AMS trays (unit and slot), HMS errors, current job, queue length."), READ),
    "druckwerk_get_queue": ("get_queue", (
        "The print queue: waiting jobs per printer serial and for any (with 0-based position), pending approvals, "
        "running jobs, optional history."), READ),
    "druckwerk_add_to_queue": ("add_to_queue", (
        "Add a job to the queue from a sliced .gcode.3mf name in the share. Does not start a print."), WRITE),
    "druckwerk_cancel_job": ("cancel_job", "Cancel a waiting job. Users cancel their own, admins any.", CANCEL),
    "druckwerk_reorder_job": ("reorder_job", (
        "Move a waiting job to a 0-based position. Users reorder only within their own slots, admins any."), WRITE),
    "druckwerk_move_job": ("move_job", "Change a waiting job's target printer, or to any. Admins only.", WRITE),
    "druckwerk_skip_job": ("skip_job", "Send a waiting job to the end of the queue. Admins only.", WRITE),
    "druckwerk_approve_job": ("approve_job", "Approve a job waiting for approval. Admins only.", WRITE),
    "druckwerk_reject_job": ("reject_job", "Reject a job waiting for approval. Admins only.", CANCEL),
    "druckwerk_retry_job": ("retry_job", "Put a failed or cancelled job back into the queue. Admins only.", WRITE),
    "druckwerk_start_next": ("start_next", "Start the next queued job on a printer." + SAFETY,
                             hints(False, destructive=True, open_world=True)),
    "druckwerk_start_job": ("start_job", "Start a chosen queued job on a printer, ahead of the queue order." + SAFETY,
                            hints(False, destructive=True, open_world=True)),
    "druckwerk_camera_snapshot": ("camera_snapshot", (
        "Take a current camera picture; saved under /media/druckwerk, returns its path. Takes up to a minute."),
        hints(True)),
}


# -- manifest
def field_schema(f):
    """JSON schema of one field for the LLM; every property carries a type."""
    kind = f["kind"]
    if kind == "bool":
        out = {"type": "boolean"}
    elif kind == "tri":
        out = {"anyOf": [{"type": "boolean"}, {"type": "string", "enum": ["auto"]}]}
    elif kind == "enum":
        out = {"type": "string", "enum": f["values"]}
    elif kind == "int":
        out = {"type": "integer", **({"minimum": f["min"]} if "min" in f else {}), **({"maximum": f["max"]} if "max" in f else {})}
    elif kind == "list":
        out = {"type": "array", "items": {"type": "string"}}
    elif kind == "options":
        out = {"type": "object", "properties": {k: field_schema(v) for k, v in OPTIONS.items()},
               "additionalProperties": False}
    else:
        out = {"type": "string", **({"maxLength": 80} if kind == "name" else {})}
    if "tool" in f:
        out["description"] = f["tool"]
    if "default" in f:
        out["default"] = f["default"]
    return out


def tool_view(name, action, description, annotations):
    fields = ACTIONS[action]["fields"]
    return {"name": name, "description": description, "annotations": annotations,
            "parameters": {"type": "object", "properties": {k: field_schema(f) for k, f in fields.items()},
                           "required": [k for k, f in fields.items() if f.get("required")]}}


def action_view(name, a):
    fields = {}
    for key, f in a["fields"].items():
        fields[key] = {"required": bool(f.get("required")),
                       **{k: f[k] for k in ("title", "description", "default", "example", "selector", "advanced") if k in f}}
    return {"name": name, **{k: a[k] for k in ("description", "response", "serial_fields")}, "title": a["title"],
            "fields": fields}


def manifest():
    body = {"api_version": API_VERSION,
            "actions": [action_view(n, a) for n, a in ACTIONS.items()],
            "entities": ENTITIES,
            "llm": {"prompt": PROMPT, "tools": [tool_view(n, *t) for n, t in TOOLS.items()]}}
    return {**body, "revision": revision(body)}


def revision(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


def manifest_revision():
    return manifest()["revision"]


# -- validation: an action coerces like Home Assistant's schemas did, a tool is strict like the LLM schemas
def invalid(text, path=""):
    raise ValueError("%s for dictionary value @ data%s" % (text, path))


def to_bool(v, path):
    if isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "on", "enable"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("0", "false", "no", "off", "disable"):
        return False
    if isinstance(v, (bool, int, float)):
        return bool(v)
    return invalid("invalid boolean value %s" % v, path)


def check(f, v, strict, path):
    kind = f["kind"]
    if kind in ("string", "name"):
        if strict and not isinstance(v, str) or isinstance(v, (list, dict)) or v is None:
            invalid("expected str" if strict else "value should be a string", path)
        v = str(v)
        if kind == "name" and len(v) > 80:
            invalid("length of value must be at most 80", path)
        return v
    if kind == "bool":
        if strict and not isinstance(v, bool):
            invalid("expected bool", path)
        return to_bool(v, path)
    if kind == "tri":
        if v == "auto":
            return v
        if strict and not isinstance(v, bool):
            invalid("not a valid value", path)
        return to_bool(v, path)
    if kind == "enum":
        if v not in f["values"]:
            invalid("value must be one of %s" % f["values"], path)
        return v
    if kind == "int":
        if strict and not isinstance(v, int):
            invalid("expected int", path)
        try:
            v = int(v)
        except (TypeError, ValueError):
            invalid("expected int", path)
        if v < f.get("min", v):
            invalid("value must be at least %d" % f["min"], path)
        if v > f.get("max", v):
            invalid("value must be at most %d" % f["max"], path)
        return v
    if kind == "list":
        items = v if isinstance(v, list) or strict else [v]
        if not isinstance(items, list):
            invalid("expected a list", path)
        return [check({"kind": "string"}, x, strict, "%s[%d]" % (path, i)) for i, x in enumerate(items)]
    return validate(OPTIONS, v, strict, path)  # kind options


def validate(fields, data, strict, path=""):
    if not isinstance(data, dict):
        invalid("expected a dictionary", path)
    for key in data:
        if key not in fields:
            raise ValueError("extra keys not allowed @ data%s['%s']" % (path, key))
    out = {}
    for key, f in fields.items():
        if key in data:
            out[key] = check(f, data[key], strict, "%s['%s']" % (path, key))
        elif f.get("required"):
            raise ValueError("required key not provided @ data%s['%s']" % (path, key))
        elif "default" in f:
            out[key] = f["default"]
    return out


def run_action(name, data, ops):
    if name not in ACTIONS:
        raise ActionError(404, "no such action")
    return getattr(ops, name)(**validate(ACTIONS[name]["fields"], data, strict=False))


def run_tool(name, data, ops):
    if name not in TOOLS:
        raise ActionError(404, "no such tool")
    action = TOOLS[name][0]
    return getattr(ops, action)(**validate(ACTIONS[action]["fields"], data, strict=True))


# -- operations
def duration(seconds):
    if seconds is None:
        return None
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours} h {rest // 60:02d} min" if hours else f"{rest // 60} min"


def job_summary(job):
    keys = ("id", "kind", "name", "file", "state", "target", "printer", "model", "owner_name", "grams", "print_seconds", "cost",
            "error", "options", "created_at", "started_at", "finished_at")
    out = {k: job[k] for k in keys if k in job}
    if job.get("print_seconds"):
        out["print_time"] = duration(job["print_seconds"])
    return out


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class Ops:
    """One call of one user. call(method, path, body=None, data=None, binary=False, timeout=30) asks the app as that user."""

    def __init__(self, call, media_dir, send_print):
        self.call, self.media_dir, self.send_print = call, media_dir, send_print

    def print_file(self, **fields):
        return self.send_print(fields)

    # -- printers
    def serial(self, printer):
        """Serial from a serial or a printer name; unknown text goes on as a serial."""
        printer = printer.strip()
        for p in self.call("GET", "/api/printers"):
            if printer == p["serial"] or printer.casefold() == str(p.get("name", "")).casefold():
                return p["serial"]
        return printer

    def get_printers(self, printer=None):
        printers = self.call("GET", "/api/printers")
        if printer:
            wanted = self.serial(printer)
            printers = [p for p in printers if p["serial"] == wanted]
            if not printers:
                raise ValueError(f"No printer {printer} visible to you")
        return {"printers": printers}

    # -- queue
    def get_queue(self, history_limit=0):
        snap = self.call("GET", "/api/queue")
        waiting, running, approvals = {}, [], []
        for job in snap["jobs"]:
            state = job.get("state")
            if state in ("starting", "printing"):
                running.append(job_summary(job))
            elif state in ("queued", "pending_approval"):
                key = job.get("target") or "any"
                row = dict(job_summary(job), position=len(waiting.setdefault(key, [])))
                waiting[key].append(row)
                if state == "pending_approval":
                    approvals.append(row)
        return {
            "waiting": waiting,  # per printer serial and "any"; position is 0-based, as reorder_job takes it
            "pending_approvals": approvals,
            "running": running,
            "history": [job_summary(j) for j in snap["history"][: max(0, history_limit)]],
            "printers": snap.get("printers", []),
        }

    def add_to_queue(self, file, target="any", model=None, options=None, name=None):
        if target != "any":
            target = self.serial(target)
        body = {"target": target, "options": options or {}}
        if name:
            body["name"] = name
        body["file"] = file
        if target == "any":
            body["model"] = model or self.only_model()
        job = self.call("POST", "/api/queue", body, timeout=QUEUE_TIMEOUT)
        return {"job": job}

    def only_model(self):
        models = {p["model"] for p in self.call("GET", "/api/printers")}
        if len(models) != 1:
            raise ValueError("target any needs model (the printer model, e.g. H2S) when printers differ")
        return models.pop()

    def job_action(self, action, job_id, **body):
        if action == "reorder" and body.get("printer"):
            body["scope"] = self.serial(body.pop("printer"))
        elif action == "move" and body.get("target") not in (None, "any"):
            body["target"] = self.serial(body["target"])
        body = {k: v for k, v in body.items() if v is not None and k != "printer"}
        job = self.call("POST", f"/api/queue/{quote(job_id, safe='')}/{action}", body)
        return {"job": job_summary(job) if isinstance(job, dict) else job}

    def cancel_job(self, job_id):
        return self.job_action("cancel", job_id)

    def skip_job(self, job_id):
        return self.job_action("skip", job_id)

    def approve_job(self, job_id):
        return self.job_action("approve", job_id)

    def reject_job(self, job_id):
        return self.job_action("reject", job_id)

    def retry_job(self, job_id):
        return self.job_action("retry", job_id)

    def reorder_job(self, job_id, position, printer=None):
        return self.job_action("reorder", job_id, position=position, printer=printer)

    def move_job(self, job_id, target, model=None):
        return self.job_action("move", job_id, target=target, model=model)

    def start(self, printer, job_id, plate_clear, confirmed_by_person):
        # Checked before any call, so a refused start never reaches the queue.
        if plate_clear is not True or confirmed_by_person is not True:
            raise ValueError(CONFIRM_TEXT)
        serial = quote(self.serial(printer), safe="")
        if job_id:
            job = self.call("POST", f"/api/printers/{serial}/start", {"job_id": job_id, "plate_clear": True}, timeout=400)
        else:
            job = self.call("POST", f"/api/printers/{serial}/start-next", {"plate_clear": True}, timeout=400)
        return {"job": job_summary(job) if isinstance(job, dict) else job}

    def start_next(self, printer, plate_clear=False, confirmed_by_person=False):
        return self.start(printer, None, plate_clear, confirmed_by_person)

    def start_job(self, printer, job_id, plate_clear=False, confirmed_by_person=False):
        return self.start(printer, job_id, plate_clear, confirmed_by_person)

    # -- camera
    def camera_snapshot(self, printer):
        serial = self.serial(printer)
        taken = time.strftime("%Y%m%d-%H%M%S")
        data = self.call("GET", f"/api/printers/{quote(serial, safe='')}/camera?mode=snapshot", binary=True, timeout=120)
        name = f"{serial}-{taken}.jpg"
        dest = os.path.join(self.media_dir, name)
        write(dest, data)
        old = sorted(n for n in os.listdir(self.media_dir) if n.startswith(serial + "-") and n.endswith(".jpg"))
        for n in old[:-SNAPSHOT_KEEP]:
            os.remove(os.path.join(self.media_dir, n))
        return {
            "printer": serial,
            "path": dest,
            "media_content_id": f"media-source://media_source/local/{MEDIA_SUBDIR}/{name}",
            "taken_at": taken,
            "size_bytes": len(data),
        }
