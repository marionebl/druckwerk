#!/usr/bin/env python3
"""Mock of the Druckwerk API for UI work. Serves hub/ui/ under a fake ingress prefix so that
absolute URLs (which break behind HA ingress) fail here too.

  python3 hub/ui/mock_server.py [port]      then open  http://127.0.0.1:8099/?as=admin|user|admin-new
All serials, names and entities are fake. State lives in memory and resets on restart."""
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PREFIX = "/api/hassio_ingress/MOCKTOKEN/"
NOW = time.time

IDENT = {
    "admin": {"user_id": "u-alex", "name": "Alex Admin", "is_admin": True, "setup_done": True},
    "admin-new": {"user_id": "u-alex", "name": "Alex Admin", "is_admin": True, "setup_done": False},
    "user": {"user_id": "u-lena", "name": "Lena Tester", "is_admin": False, "setup_done": True},
}
USERS = {"u-alex": "Alex Admin", "u-lena": "Lena Tester", "u-jonas": "Jonas Example"}
# serial -> printer; "t0" is when the fake print started
SERIALS = {"H2S": "FAKE-H2S-0001", "P1S": "FAKE-P1S-0002", "X1C": "FAKE-X1C-0003"}
H2S, P1S, X1C = SERIALS["H2S"], SERIALS["P1S"], SERIALS["X1C"]
USER_PRINTERS = {H2S: "user", P1S: "user"}


def tray(slot, type_, colour, name, pct, spool=None):
    return {"slot": slot, "type": type_, "colour": colour, "name": name, "remain_pct": pct, "spool_id": spool}


PRINTERS = {
    H2S: {"serial": H2S, "name": "Workshop H2S", "model": "H2S", "online": True, "status": "running", "stage": "printing",
          "progress": 42, "remaining_min": 95, "layer": 128, "total_layers": 300, "nozzle_temp": 249, "nozzle_target": 250,
          "bed_temp": 59.6, "bed_target": 60, "chamber_temp": 38, "task_name": "Gridfinity bin 4x2",
          "active_tray": {"unit": 0, "slot": 1}, "light": False,
          "ams": [{"unit": 0, "humidity_index": 4, "temp": 27, "trays": [
              tray(0, "PLA", "F2F2F2", "Acme PLA White", 88, "5000"), tray(1, "PETG", "1F6FEB", "Prusament PETG Blue", 61, "5001"),
              tray(2, "PLA", "E4572E", "Sunlu PLA Red", 14, "5002"), tray(3, "", "", "", -1)]},
              {"unit": 1, "humidity_index": 2, "temp": 31, "trays": [tray(0, "TPU", "222222", "TPU 95A Black", 73, "5003")]}],
          "external": None, "hms": []},
    P1S: {"serial": P1S, "name": "Office P1S", "model": "P1S", "online": True, "status": "idle", "stage": "idle",
          "progress": 0, "remaining_min": 0, "layer": 0, "total_layers": 0, "nozzle_temp": 31, "nozzle_target": 0,
          "bed_temp": 29, "bed_target": 0, "chamber_temp": 27, "task_name": "", "active_tray": None,
          "ams": [{"unit": 0, "humidity_index": 5, "temp": 24, "trays": [
              tray(0, "PLA", "2E2E2E", "Generic PLA Black", 95), tray(1, "PLA", "F2C94C", "Generic PLA Yellow", 40),
              tray(2, "", "", "", -1), tray(3, "", "", "", -1)]}],
          "external": None, "hms": []},
    X1C: {"serial": X1C, "name": "Garage X1C", "model": "X1C", "online": False, "status": "offline", "stage": "offline",
          "progress": 0, "remaining_min": 0, "layer": 0, "total_layers": 0, "nozzle_temp": 22, "nozzle_target": 0,
          "bed_temp": 21, "bed_target": 0, "chamber_temp": 21, "task_name": "", "active_tray": None, "ams": [],
          "external": {"type": "PLA", "colour": "3FB68B", "name": "Spool on holder"},
          "hms": [{"code": "0300-0100-0001-0007", "severity": "serious", "text": "The nozzle temperature is abnormal; the heater may be loose.",
                   "url": "https://example.com/hms/0300_0100_0001_0007"}]},
}
T0 = {H2S: NOW() - 58 * 60}
# Test hooks the UI check switches: a printer whose camera answers 502, one whose AMS slots are all empty, what was sent.
CAMERA_DOWN, EMPTY_AMS, MOCK_SLICES, MOCK_LIGHT = set(), set(), [], []
# AMS units with a dryer, per serial; a drying unit counts down from "until".
DRYING = {H2S: {0: {"name": "AMS 2 Pro", "lo": 45, "hi": 65, "until": None, "filament": "", "temp": None},
                128: {"name": "AMS HT", "lo": 45, "hi": 85, "until": None, "filament": "", "temp": None}}}
RESTART = {"until": 0.0}  # while NOW() is before this, every API call answers like Home Assistant that restarts
MOCK_QUEUED = []  # what the UI sent to queue a slice, for the tests


def drying_out(s):
    out = []
    for ams_id, u in DRYING.get(s, {}).items():
        left = max(0, int((u["until"] - NOW()) / 60)) if u["until"] else 0
        active = bool(u["until"] and u["until"] > NOW())
        out.append({"ams_id": ams_id, "name": u["name"], "status": "drying" if active else "off", "active": active, "left_min": left,
                    "filament": u["filament"] if active else "", "temp": u["temp"] if active else None, "hours": None,
                    "temp_min": u["lo"], "temp_max": u["hi"]})
    return out


def job(id_, file, target, owner, state, model="", **kw):
    j = {"id": id_, "kind": "print", "name": file.replace(".gcode.3mf", ""), "file": file, "source": file.replace(".gcode.3mf", ""), "target": target, "model": model,
         "filaments": [{"type": "PLA", "colour": "F2F2F2"}], "options": {"timelapse": False, "bed_leveling": True, "use_ams": True, "flow_cali": True, "vibration_cali": True,
                                        "layer_inspect": False, "nozzle_offset_cali": False, "bed_type": "auto", "ext_change_assist": False},
         "state": state, "error": None, "created_at": NOW() - 3600 * kw.pop("age", 2), "owner_user_id": owner, "owner_name": USERS[owner],
         "plate_png": f"api/queue/{id_}/plate.png"}
    j.update(kw)
    return j



JOBS = [
    job("a00000000001", "gridfinity_bin_4x2.gcode.3mf", H2S, "u-alex", "printing", printer=H2S, started_at=NOW() - 3500, grams=38.2, cost=0.84),
    job("a00000000002", "cable_clip_set.gcode.3mf", H2S, "u-lena", "queued", grams=12.5, cost=0.31),
    job("a00000000003", "phone_stand_v3.gcode.3mf", H2S, "u-jonas", "queued", grams=55.0, cost=1.20),
    job("a00000000004", "bracket_shelf_left.gcode.3mf", "any", "u-jonas", "queued", model="P1S", grams=71.4, cost=1.62),
    job("a00000000005", "toy_dragon_articulated.gcode.3mf", P1S, "u-lena", "pending_approval", grams=96.0, cost=2.10),
    job("a00000000006", "drawer_divider_s.gcode.3mf", P1S, "u-alex", "queued", grams=8.9, cost=0.22),
]
JOBS[1]["name"] = "Cable clips for the desk"


def slice_of(quality, seconds, grams, model):
    return {"quality": quality, "process": "0.20mm Standard @BBL H2S", "filament": "Bambu PLA Basic @BBL H2S", "bed": "Textured PEI Plate",
            "tray": {"ams": "0", "id": "0", "colour": "F2F2F2", "type": "PLA"}, "seconds": seconds, "grams": grams, "model": model,
            "filaments": [{"type": "PLA", "colour": "F2F2F2", "grams": grams}]}


SLICE_JOB = "d00000000001"  # a slice the slicer still keeps; the other jobs' slices are gone
JOBS[1].update(slice_job=SLICE_JOB, slice=slice_of("better", 2520, 12.5, "cable_clip_set.stl"), print_seconds=2520,
               options=dict(JOBS[1]["options"], timelapse=True, flow_cali="auto"))
JOBS[0].update(slice=slice_of("good", 6200, 38.2, "gridfinity_bin_4x2.stl"), print_seconds=6200)


def dry_job(id_, target, owner, ams_id, temp, hours, filament, state="queued", **kw):
    unit = "AMS %d" % (ams_id + 1) if ams_id < 128 else "AMS HT"
    label = "Dry %s (%s)" % (unit, filament)
    j = {"id": id_, "kind": "dry", "name": label, "file": "", "source": label, "target": target, "model": "", "filaments": [], "options": {},
         "dry": {"ams_id": ams_id, "temp": temp, "hours": hours, "filament": filament}, "state": state, "error": None,
         "created_at": NOW() - 600, "owner_user_id": owner, "owner_name": USERS[owner], "plate_png": None}
    j.update(kw)
    return j


JOBS.append(dry_job("a00000000007", H2S, "u-alex", 0, 55, 8, "PLA"))

HISTORY = [
    job("b00000000001", "benchy_test.gcode.3mf", H2S, "u-lena", "done", printer=H2S, grams=14.0, cost=0.35, age=30, finished_at=NOW() - 3600 * 28),
    job("b00000000002", "hook_wall_large.gcode.3mf", P1S, "u-jonas", "failed", printer=P1S, grams=3.1, cost=0.08, age=50,
        error="printer reported a failed print", finished_at=NOW() - 3600 * 47),
    job("b00000000003", "lens_cap.gcode.3mf", P1S, "u-alex", "cancelled", age=70, finished_at=NOW() - 3600 * 70),
    # never reached a printer: no cloud task explains it
    job("b00000000004", "wall_hook_test.gcode.3mf", H2S, "u-lena", "failed", age=5, finished_at=NOW() - 3600 * 4.5,
        error="The printer is busy; the job was not sent."),
    job("b00000000005", "cable_clip_set.gcode.3mf", H2S, "u-lena", "done", printer=H2S, grams=12.5, cost=0.31, age=4, finished_at=NOW() - 3600 * 2.8,
        started_at=NOW() - 3.5 * 3600, print_seconds=2520, spools_used=[{"spool_id": "5000", "grams": 12.5}]),
]
HISTORY[0].update(name="Benchy for the shelf", started_at=NOW() - 28 * 3600, print_seconds=3660, spools_used=[{"spool_id": "5000", "grams": 14.0}, {"spool_id": "5001", "grams": 0.4}],
                  slice=slice_of("better", 3660, 14.0, "benchy.stl"), options=dict(HISTORY[0]["options"], timelapse=True))
HISTORY[4].update(name="Cable clip set (desk)", slice_job=SLICE_JOB, slice=slice_of("best", 2520, 12.5, "cable_clip_set.stl"),
                  options=dict(HISTORY[4]["options"], timelapse=True, flow_cali="auto", bed_leveling=False))
HISTORY[3].pop("printer", None)
TASK_JOB = {5: "b00000000001", 4: "b00000000002", 3: "b00000000003", 8: "b00000000005"}
SLOW = {}  # test hook: path fragment -> seconds to wait before answering
# Cloud tasks: (id, title, printer, hours ago, status, minutes, grams, user id or None for "not started here", cost)
def _yesterday_at(hour):  # fixed local clock times, so the two timeline prints never cross midnight whenever the check runs
    t = time.localtime()
    return NOW() - time.mktime((t.tm_year, t.tm_mon, t.tm_mday - 1, hour, 0, 0, 0, 0, -1))


TASKS = [
    (11, "Timeline one hour", H2S, _yesterday_at(8) / 3600, "finished", 60, 20.0, "u-lena", 0.4),
    (12, "Timeline three hours", H2S, _yesterday_at(13) / 3600, "finished", 180, 50.0, "u-lena", 1.0),
    (9, "gridfinity_bin_4x2", H2S, 0.9, "running", 0, 0, "u-alex", None),
    (8, "cable_clip_set", H2S, 3.5, "finished", 42, 12.5, "u-lena", 0.31),
    (7, "Slicer plate 1", P1S, 5, "finished", 95, 61.0, None, None),
    (6, "phone_stand_v3", H2S, 20, "finished", 130, 55.0, "u-jonas", 1.2),
    (5, "benchy_test", H2S, 28, "finished", 61, 14.0, "u-lena", 0.35),
    (4, "hook_wall_large", P1S, 47, "failed", 12, 3.1, "u-jonas", 0.08),
    (3, "lens_cap", P1S, 70, "cancelled", 4, 0.6, "u-alex", None),
    (2, "calibration cube", P1S, 75, "finished", 28, 6.2, None, None),
    (1, "drawer_divider_s", H2S, 150, "finished", 20, 8.9, "u-alex", 0.22),
    (10, "Slicer plate 3", H2S, 9, "finished", 33, 17.0, None, None),
]
OWNERS = {H2S: "u-jonas"}  # printer owner (HA user id); the P1S has none
BROKEN_COVERS = (7,)  # cloud covers whose image does not load


def history_out(ident, q):
    """Same rules as the hub: admins see all and may filter; others only their own prints on their printers."""
    admin, uid = IDENT[ident]["is_admin"], IDENT[ident]["user_id"]
    out = []
    for id_, title, serial, ago, status, minutes, grams, user, cost in TASKS:
        if serial not in visible(ident) or (q.get("printer") and q["printer"][0] != serial):
            continue
        if admin:
            if q.get("user") and q["user"][0] != (user or "unknown"):
                continue
        elif user != uid:
            continue
        started = NOW() - ago * 3600
        jb = next((j for j in HISTORY if j["id"] == TASK_JOB.get(id_)), None)
        out.append({"id": id_, "serial": serial, "title": title, "status": status, "status_code": status, "started_at": int(started),
                    "ended_at": None if status == "running" else int(started + minutes * 60), "seconds": minutes * 60 or None,
                    "grams": grams or None, "cover_url": f"api/history-cover/{id_}.svg" if id_ % 2 else "", "plate": 1, "design_title": "",
                    "printer_name": PRINTERS[serial]["name"], "job_id": None, "user_id": user, "user_name": USERS.get(user),
                    "cost": cost, "source": "druckwerk" if user else "cloud", "job": jb and job_view(jb), "when": int(started)})
        if not user:
            out[-1]["printer_owner"] = USERS.get(OWNERS.get(serial))
        if jb:
            out[-1].update(job_id=jb["id"], user_id=jb["owner_user_id"], user_name=jb["owner_name"], cost=jb.get("cost"))
    matched = set(TASK_JOB.values())
    for j in HISTORY:
        if j["id"] in matched or not sees(ident, j) or (not admin and j["owner_user_id"] != uid):
            continue
        if q.get("printer") and q["printer"][0] != (j.get("printer") or j["target"]):
            continue
        if admin and q.get("user") and q["user"][0] != j["owner_user_id"]:
            continue
        out.append({"id": "job-" + j["id"], "serial": j["target"], "title": j["name"], "status": {"done": "finished"}.get(j["state"], j["state"]),
                    "started_at": j.get("started_at"), "ended_at": int(j["finished_at"]), "seconds": j.get("print_seconds"), "grams": j.get("grams"),
                    "cover_url": None, "printer_name": PRINTERS[j["target"]]["name"], "job_id": j["id"], "user_id": j["owner_user_id"],
                    "user_name": j["owner_name"], "cost": j.get("cost"), "source": "job", "error": j["error"], "job": job_view(j),
                    "when": int(j.get("started_at") or j["finished_at"])})
    out.sort(key=lambda i: i["when"], reverse=True)
    return {"items": out, "unavailable": []}


SETTINGS_ALL = [["curr_bed_type", "Textured PEI Plate"], ["filament_flow_ratio", "0.98"], ["layer_height", "0.2"], ["nozzle_diameter", "0.4"],
                ["outer_wall_speed", "200 / 500"], ["printer_model", "Bambu Lab H2S"], ["sparse_infill_density", "15%"], ["wall_loops", "2"]]
SETTINGS_GROUPS = [
    ("Quality", [("Layer height", "0.2 mm"), ("First layer height", "0.2 mm"), ("Outer wall line width", "0.42 mm"), ("Seam position", "aligned")]),
    ("Strength", [("Wall loops", "2"), ("Top shell layers", "5"), ("Bottom shell layers", "3"), ("Infill density", "15%"), ("Infill pattern", "grid")]),
    ("Support and adhesion", [("Support", "Off"), ("Brim", "auto_brim"), ("Prime tower", "On")]),
    ("Speed", [("Outer wall", "200 / 500 mm/s"), ("Infill", "350 / 600 mm/s"), ("Travel", "1000 mm/s")]),
    ("Filament", [("Preset", "Bambu PLA Basic @BBL H2S"), ("Type", "PLA"), ("Nozzle temperature", "220 °C"), ("Flow ratio", "0.98")]),
    ("Plate and bed", [("Build plate", "Textured PEI Plate"), ("Bed temperature", "55 °C")]),
    ("Printer", [("Printer model", "Bambu Lab H2S"), ("Nozzle diameter", "0.4 mm"), ("Process preset", "0.20mm Standard @BBL H2S")])]
# task id -> (source, can_reprint) of the settings the mock has; other items have none
SETTINGS_OF = {"8": ("slice", True), "5": ("saved", False), "10": ("cloud", True)}


# Files the hub still holds per cloud task: (kind, name). Item 8 has its model, 10 only the cloud's sliced file; the others have none.
FILES_OF = {"8": ("model", "cable_clip_set.stl"), "10": ("sliced", "Slicer_plate_3.gcode.3mf")}


def settings_out(item):
    src, again = SETTINGS_OF.get(str(item["id"]), (None, False))
    task = {k: item[k] for k in ("title", "design_title", "plate", "status_code") if item.get(k) not in (None, "")}
    kind, fname = FILES_OF.get(str(item["id"]), (None, None))
    dl = {"download": kind and {"kind": kind, "name": fname}, "preview": bool(kind)}
    if not src:
        return {**dl, "available": False, "source": None, "can_reprint": False, "task": dict(task, model_id="US1", profile_id=4711) if item["source"] == "cloud" else task,
                "reason": "The Bambu cloud did not give the file (cloud file: HTTP Error 404)." if item["source"] == "cloud" else "The sliced file is gone.",
                "groups": [], "all": [], "plate": {}, "slicer": None}
    return {**dl, "available": True, "source": src, "reason": "", "can_reprint": again, "task": task, "slicer": "02.09.02.62", "plate": {},
            "groups": [{"title": t, "rows": [{"key": l, "label": l, "value": v, "unit": ""} for l, v in rows]} for t, rows in SETTINGS_GROUPS],
            "all": SETTINGS_ALL}


JOB_FIELDS = ("id", "name", "state", "error", "file", "source", "target", "model", "filaments", "options", "spools_used", "grams", "print_seconds",
              "cost", "plate_png", "slice_job", "slice", "owner_user_id", "owner_name", "created_at", "started_at", "finished_at")


def job_view(j):
    return {k: j[k] for k in JOB_FIELDS if j.get(k) is not None}


# The Bambu Cloud filament library as the hub lists it; prices live in the hub (PRICES), the cloud has none.
def cloud_spool(id_, brand, type_, name, colour, left, total, loaded=None):
    p, u, sl = loaded or (None, None, None)
    return {"id": id_, "brand": brand, "type": type_, "name": name, "colour": colour, "grams_left": left, "grams_total": total,
            "printer": p, "unit": u, "slot": sl, "in_printer": p is not None, "rfid": None, "account": "acc1"}


SPOOLS = [
    cloud_spool("5000", "Acme", "PLA", "PLA Basic White", "F2F2F2", 880, 1000, (H2S, 0, 0)),
    cloud_spool("5001", "Prusament", "PETG", "PETG Blue", "1F6FEB", 610, 1000, (H2S, 0, 1)),
    cloud_spool("5002", "Sunlu", "PLA", "PLA Red", "E4572E", 140, 1000, (H2S, 0, 2)),
    cloud_spool("5003", "Generic", "TPU", "TPU 95A Black", "222222", 730, 1000, (H2S, 1, 0)),
    cloud_spool("5004", "Bambu Lab", "PLA", "PLA Matte Charcoal", "2E2E2E", None, None),
]
PRICES = {"5000": 19.99, "5001": 27.5, "5002": 14.0, "5003": 32.0}
LOAD_REQUESTS = []  # open "ask to load" requests
SPOOLS_DOWN = {"on": False}  # test hook: the cloud library does not answer
GRANTS = [
    {"id": "c00000000001", "user_id": "u-lena", "user_name": "Lena Tester", "printer": H2S, "role": "user",
     "until": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW() + 86400 * 30)), "condition_entity": "schedule.workshop_hours", "needs_approval": False, "active": True},
    {"id": "c00000000002", "user_id": "u-jonas", "user_name": "Jonas Example", "printer": "*", "role": "user",
     "until": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW() - 86400)), "condition_entity": None, "needs_approval": True, "active": False},
]
HA_USERS = [{"id": "u-alex", "name": "Alex Admin", "is_admin": True}, {"id": "u-lena", "name": "Lena Tester", "is_admin": False},
            {"id": "u-jonas", "name": "Jonas Example", "is_admin": False}]
ENTITIES = [
    {"entity_id": "schedule.workshop_hours", "name": "Workshop hours", "state": "on"},
    {"entity_id": "input_boolean.guests_home", "name": "Guests home", "state": "off"},
    {"entity_id": "binary_sensor.workshop_door", "name": "Workshop door", "state": "off"},
    {"entity_id": "person.lena", "name": "Lena", "state": "home"},
]
SETUP = {"accounts": [{"id": "acc1", "email": "owner@example.invalid", "logged_in": True, "daemon_running": True}],
         "printers": {s: {"serial": s, "device_name": PRINTERS[s]["name"], "model": PRINTERS[s]["model"], "account": "acc1",
                          "configured": s != X1C, "config": None, "suggested_prefix": {H2S: "h2s_fake0001", P1S: "p1s_fake0002"}.get(s)} for s in PRINTERS}}
FEEDBACK = []  # what the feedback button posted, for the UI proof
SLICES = {SLICE_JOB: {"id": SLICE_JOB, "filename": "cable_clip_set.stl", "state": "done", "sliced_at": 1.0, "quality": "better", "tray": "0:0", "bed": "Textured PEI Plate"}}
LOCK = threading.Lock()
QUALITY = {"good": (7800, 41.0, "0.24mm Draft"), "better": (10500, 42.5, "0.20mm Standard"), "best": (14700, 44.1, "0.16mm High Quality")}
WAITING = ("queued", "pending_approval")


def hue(s):
    return int(hashlib.md5(s.encode()).hexdigest()[:4], 16) % 360


def plate_svg(seed):
    c = hue(seed)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120"><rect width="120" height="120" fill="#e9ecef"/>'
            f'<path d="M20 78l40-22 40 22-40 22z" fill="#cfd4da"/><path d="M34 70l26-14 26 14v22l-26 14-26-14z" fill="hsl({c} 60% 55%)"/>'
            f'<path d="M34 70l26-14 26 14-26 14z" fill="hsl({c} 60% 68%)"/></svg>')


def camera_svg(p):
    t = int(NOW()) % 60
    if p["status"] == "running":
        h = min(80, 20 + p["progress"] * 0.8)
        obj = f'<rect x="250" y="{210 - h}" width="140" height="{h}" rx="6" fill="#e4572e"/><rect x="250" y="{210 - h}" width="140" height="6" fill="#f08a6a"/>'
        nozzle = f'<rect x="305" y="{150 - h}" width="30" height="40" fill="#555"/><rect x="316" y="{185 - h}" width="8" height="12" fill="#d8a"/>'
    else:
        obj, nozzle = "", '<rect x="305" y="60" width="30" height="40" fill="#555"/>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360"><rect width="640" height="360" fill="#23272d"/>'
            f'<rect x="40" y="20" width="560" height="10" fill="#3a4048"/><path d="M110 270l60-70h300l60 70z" fill="#3b4a40"/>'
            f'<rect x="110" y="270" width="420" height="14" fill="#2c3238"/>{obj}{nozzle}'
            f'<text x="16" y="344" font-size="14" fill="#8b949e" font-family="monospace">mock camera {t:02d}s</text></svg>')


def gcode():
    if os.environ.get("MOCK_GCODE"):  # try the viewer on a real file
        return open(os.environ["MOCK_GCODE"], encoding="latin-1").read()
    """Two objects the way Bambu Studio writes them: label markers, ; FEATURE: comments, config tail with the plate."""
    out = ["; mock gcode", "M83"]
    box = {1: (40, 40, 100, 100, 60), 2: (150, 130, 210, 190, 40)}  # x0, y0, x1, y1, layers
    for n in range(60):
        z = 0.2 * (n + 1)
        out += ["; CHANGE_LAYER", f"; Z_HEIGHT: {z:.2f}", f"G1 Z{z:.2f}"]
        for oid, (x0, y0, x1, y1, layers) in box.items():
            if n >= layers:
                continue
            out += [f"; OBJECT_ID: {oid}", f"; start printing object, unique label id: {oid}", "M624 AQAAAAAAAAA="]
            out.append(f"G1 X{x0} Y{y0} F9000")  # travel
            for feat, inset in (("Outer wall", 0), ("Inner wall", 0.8)):
                out.append(f"; FEATURE: {feat}")
                a, b, c, d = x0 + inset, y0 + inset, x1 - inset, y1 - inset
                out.append(f"G1 X{a} Y{b}")
                for x, y in ((c, b), (c, d), (a, d), (a, b)):
                    out.append(f"G1 X{x} Y{y} E0.05")
            out.append("; FEATURE: Top surface" if n == layers - 1 else "; FEATURE: Sparse infill")
            for k in range(x0 + 4, x1 - 3, 6):
                out += [f"G1 X{k} Y{y0 + 4}", f"G1 X{k + (n % 3) * 2} Y{y1 - 4} E0.04"]
            if oid == 2 and n < 8:
                out.append("; FEATURE: Support")
                out += [f"G1 X{x0 + 10} Y{y0 + 10}", f"G1 X{x1 - 10} Y{y1 - 10} E0.04"]
            out += ["; stop printing object, unique label id: " + str(oid), "M625"]
    out += ["; CONFIG_BLOCK_START", "; printable_area = 0x0,256x0,256x256,0x256", "; CONFIG_BLOCK_END"]
    return "\n".join(out)


def me(ident):
    base = dict(IDENT[ident])
    base["printers"] = [] if base["is_admin"] else [{"serial": s, "role": r} for s, r in USER_PRINTERS.items()]
    if base["is_admin"]:
        base["printers"] = [{"serial": s, "role": "admin"} for s in PRINTERS]
    return base


def visible(ident):
    return list(PRINTERS) if IDENT[ident]["is_admin"] else list(USER_PRINTERS)


def printer_out(ident, p):
    p = json.loads(json.dumps(p))
    s = p["serial"]
    p["role"] = "admin" if IDENT[ident]["is_admin"] else USER_PRINTERS.get(s)
    p["account"] = "acc1"
    if s in EMPTY_AMS:
        for u in p["ams"]:
            u["trays"] = [tray(t["slot"], "", "", "", -1) for t in u["trays"]]
        p["active_tray"] = None
    if p["status"] == "running":
        elapsed = (NOW() - T0[s]) / 60
        p["remaining_min"] = max(1, int(95 - (elapsed - 58)))
        p["end_time"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW() + p["remaining_min"] * 60))
    else:
        p["end_time"] = None
    p["cover_url"] = f"api/printers/{s}/cover" if p["online"] else None
    p["camera_url"] = f"api/printers/{s}/camera" if p["online"] else None
    p["current_job"] = next((j for j in JOBS if j["state"] == "printing" and j.get("printer") == s), None)
    p["drying"] = drying_out(s)
    p["queue_length"] = sum(1 for j in JOBS if j["state"] in WAITING and (j["target"] == s or (j["target"] == "any" and j["model"] == p["model"])))
    return p


def sees(ident, j):
    if IDENT[ident]["is_admin"]:
        return True
    vis = visible(ident)
    return j["target"] in vis or (j["target"] == "any" and any(PRINTERS[s]["model"] == j["model"] for s in vis))


def start_dry(serial, d):
    u = DRYING.get(serial, {}).get(d.get("ams_id"))
    if not u:
        raise Err(400, "This printer reports no dryer on AMS unit %s." % d.get("ams_id"))
    if not (isinstance(d.get("temp"), int) and u["lo"] <= d["temp"] <= u["hi"]):
        raise Err(400, "temp must be %d to %d for %s." % (u["lo"], u["hi"], u["name"]))
    if not (isinstance(d.get("hours"), int) and 1 <= d["hours"] <= 72):
        raise Err(400, "hours must be 1 to 72.")
    u.update(until=NOW() + d["hours"] * 3600, filament=d.get("filament", ""), temp=d["temp"])


class Err(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


def is_admin_for(ident, j):
    return IDENT[ident]["is_admin"]


def find(id_):
    for j in JOBS:
        if j["id"] == id_:
            return j
    raise Err(404, "no such job")


def job_action(ident, jid, action, body):
    uid = IDENT[ident]["user_id"]
    j = find(jid) if action != "retry" else next((x for x in HISTORY if x["id"] == jid), None)
    if j is None:
        raise Err(404, "no such finished job")
    own, admin = j["owner_user_id"] == uid, IDENT[ident]["is_admin"]
    if action == "cancel":
        if not (own or admin):
            raise Err(403, "rule: users may only cancel their own jobs; admins cancel any job")
        j["state"] = "cancelled"; JOBS.remove(j); HISTORY.insert(0, j); j["finished_at"] = NOW()
    elif action == "reorder":
        if not (own or admin):
            raise Err(403, "rule: users may only reorder their own jobs; admins reorder any job")
        pos = body.get("position")
        if not isinstance(pos, int):
            raise Err(400, "position must be an integer")
        slots = [i for i, x in enumerate(JOBS) if x["state"] in WAITING and (admin or x["owner_user_id"] == uid)
                 and (not body.get("scope") or body["scope"] in (x["target"], x.get("printer")))]
        pool = [JOBS[i] for i in slots]
        if j not in pool:
            raise Err(409, "job is not in this reorder scope")
        pool.remove(j); pool.insert(max(0, min(pos, len(pool))), j)
        for i, x in zip(slots, pool):
            JOBS[i] = x
    elif action == "stop":
        if not admin or j.get("kind") != "dry" or j["state"] != "drying":
            raise Err(403 if not admin else 409, "rule: only admins stop a drying" if not admin else "this job is not drying")
        DRYING[j["printer"]][j["dry"]["ams_id"]]["until"] = None
        j.update(state="cancelled", error="stopped", finished_at=NOW()); JOBS.remove(j); HISTORY.insert(0, j)
    elif action in ("approve", "reject", "skip", "move"):
        if not admin:
            raise Err(403, "rule: only admins of the printer may %s jobs" % action)
        if action == "approve":
            j["state"] = "queued"
        elif action == "reject":
            j["state"] = "cancelled"; j["error"] = "rejected by an admin"; JOBS.remove(j); HISTORY.insert(0, j); j["finished_at"] = NOW()
        elif action == "skip":
            JOBS.remove(j); JOBS.append(j)
        else:
            j["target"], j["model"] = body.get("target", "any"), body.get("model", "")
    elif action == "edit":
        if not (own or admin):
            raise Err(403, "rule: users may only edit their own jobs; admins edit any job")
        if j["state"] not in WAITING:
            raise Err(409, "only waiting jobs can be edited")
        if body.get("name") is not None:
            j["name"] = body["name"].strip()[:80] or j["name"]
        j["options"] = dict(j["options"], **(body.get("options") or {}))
    elif action == "retry":
        if not (own or admin):
            raise Err(403, "rule: users may only retry their own jobs")
        HISTORY.remove(j); j.update(state="queued", error=None); JOBS.append(j)
    return j


def make_job_state(sl):
    """A fake slice finishes 2 s after /slice."""
    if sl.get("sliced_at") is None:
        return {"state": "uploaded"}
    el = NOW() - sl["sliced_at"]
    if el < 2.0:
        return {"state": "slicing", "elapsed": int(el)}
    sec, g, proc = QUALITY[sl["quality"]]
    return {"state": "done", "quality": sl["quality"], "info": {"seconds": sec, "grams": g, "warnings": [], "has_png": True, "filaments": []},
            "settings": {"process": proc + " @BBL", "tray": sl["tray"], "bed": sl["bed"]}}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        if os.environ.get("MOCK_LOG"):
            sys.stderr.write("%s\n" % (fmt % a))

    def send(self, code, body, ctype="application/json", extra=()):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        return raw

    def js(self, raw):
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise Err(400, "bad JSON")

    def handle_any(self, method):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        ident = (q.get("as") or [cookies["as"].value if "as" in cookies else "admin"])[0]
        if ident not in IDENT:
            ident = "admin"
        extra = [("Set-Cookie", f"as={ident}; Path=/")] if "as" in q else []
        if u.path == "/favicon.ico":  # the system Chrome asks for it; a 404 would count as a console error
            return self.send(204, b"")
        if u.path == "/" or u.path == PREFIX.rstrip("/"):
            return self.send(302, b"", extra=[("Location", PREFIX + ("?as=" + ident if "as" in q else ""))] + extra)
        if not u.path.startswith(PREFIX):
            return self.send(404, {"error": "outside the ingress prefix: " + u.path})
        path = u.path[len(PREFIX):]
        raw = self.body()
        if path == "api/_mock/restart":  # test hook: answer 502 for ?s= seconds
            RESTART["until"] = NOW() + float((q.get("s") or ["6"])[0])
            return self.send(200, {"until": RESTART["until"]})
        if path.startswith("api/") and NOW() < RESTART["until"]:
            return self.send(502, b"<html><body>502: Bad Gateway</body></html>", "text/html")
        try:
            for frag, secs in list(SLOW.items()):  # outside the lock: a slow answer must not block the others
                if frag in u.path:
                    time.sleep(secs)
            with LOCK:
                if path.startswith("api/"):
                    code, out, ct = self.api(method, path, q, raw, ident)
                    return self.send(code, out, ct, extra)
            return self.static(path or "index.html", extra)
        except Err as e:
            return self.send(e.code, {"error": e.msg}, extra=extra)

    def static(self, rel, extra):
        f = os.path.normpath(os.path.join(HERE, rel))
        if not f.startswith(HERE + os.sep) or f.endswith(".py") or not os.path.isfile(f):
            return self.send(404, {"error": "not found"})
        ct = mimetypes.guess_type(f)[0] or "application/octet-stream"
        with open(f, "rb") as fh:
            self.send(200, fh.read(), ct, extra)

    def api(self, m, path, q, raw, ident):
        J, admin = (lambda: self.js(raw)), IDENT[ident]["is_admin"]
        uid = IDENT[ident]["user_id"]
        ok = lambda o: (200, o, "application/json")
        svg = lambda s: (200, s.encode(), "image/svg+xml")
        need_admin = lambda: None if admin else (_ for _ in ()).throw(Err(403, "rule: only Home Assistant admins can do this"))
        if path == "api/_mock/slow":
            SLOW.clear()
            for frag in q.get("path") or []:
                SLOW[frag] = float((q.get("s") or ["2"])[0])
            return ok(SLOW)
        if path == "api/me":
            return ok(me(ident))
        if path == "api/printers" and m == "GET":
            return ok([printer_out(ident, PRINTERS[s]) for s in visible(ident)])
        if path == "api/_mock/queued":
            return ok(MOCK_QUEUED)
        if path == "api/_mock/hook":  # ?set=camera_down|empty_ams&serial=..&on=1|0
            target = {"camera_down": CAMERA_DOWN, "empty_ams": EMPTY_AMS}[q["set"][0]]
            (target.add if (q.get("on") or ["1"])[0] == "1" else target.discard)(q["serial"][0])
            return ok({})
        if path == "api/_mock/slices":
            return ok(MOCK_SLICES)
        if path == "api/_mock/light":
            return ok(MOCK_LIGHT)
        r = re.fullmatch(r"api/printers/([^/]+)/light", path)
        if r and m == "POST":
            b = J()
            if r.group(1) not in visible(ident):
                raise Err(404, "no such printer")
            if not admin:
                raise Err(403, "rule: only an admin of the printer can switch its light")
            PRINTERS[r.group(1)]["light"] = b["on"]
            MOCK_LIGHT.append({"serial": r.group(1), "on": b["on"]})
            return ok({"on": b["on"]})
        r = re.fullmatch(r"api/printers/([^/]+)/(camera|cover)", path)
        if r:
            if r.group(1) not in visible(ident):
                raise Err(404, "no such printer")
            if r.group(1) in CAMERA_DOWN:
                raise Err(502, "waiting for camera URL callback")
            return svg(camera_svg(PRINTERS[r.group(1)]))
        r = re.fullmatch(r"api/printers/([^/]+)/(start-next|start)", path)
        if r and m == "POST":
            s, b = r.group(1), J()
            if not admin:
                raise Err(403, "rule: only admins of the printer may start prints")
            nxt = next((j for j in JOBS if j["id"] == b.get("job_id")), None) if r.group(2) == "start" else \
                next((j for j in JOBS if j["state"] == "queued" and (j["target"] == s or j["target"] == "any")), None)
            if not nxt:
                raise Err(409, "no job to start")
            if nxt.get("kind") == "dry":
                if b.get("confirmed") is not True:
                    raise Err(409, "confirm the start of the drying")
                start_dry(s, nxt["dry"])
                nxt.update(state="drying", printer=s, started_at=NOW())
                return ok(nxt)
            if b.get("plate_clear") is not True:
                raise Err(409, "confirm that the plate is clear")
            nxt.update(state="printing", printer=s, started_at=NOW())
            return ok(nxt)
        r = re.fullmatch(r"api/printers/([^/]+)/drying(/stop)?", path)
        if r and m == "POST":
            s, b = r.group(1), J()
            if not admin or s not in visible(ident):
                raise Err(403, "rule: only an admin of the printer can dry an AMS")
            if r.group(2):
                DRYING.get(s, {}).get(b.get("ams_id"), {})["until"] = None
                for j in [x for x in JOBS if x.get("kind") == "dry" and x["state"] == "drying" and x.get("printer") == s]:
                    j.update(state="cancelled", error="stopped", finished_at=NOW()); JOBS.remove(j); HISTORY.insert(0, j)
                return ok({"stopped": b.get("ams_id")})
            if b.get("confirmed") is not True:
                raise Err(409, "confirm the start of the drying")
            start_dry(s, b)
            jb = dry_job(secrets.token_hex(6), s, uid, b["ams_id"], b["temp"], b["hours"], b.get("filament", ""), "drying", printer=s, started_at=NOW())
            JOBS.append(jb)
            return ok(jb)
        if path == "api/queue" and m == "POST":
            b = J()
            if b.get("kind") != "dry":
                raise Err(400, "file must be a .gcode.3mf name in the share")
            if not (admin or visible(ident)):
                raise Err(403, "rule: adding a job needs an active grant on the target printer")
            jb = dry_job(secrets.token_hex(6), b["target"], uid, b["ams_id"], b["temp"], b["hours"], b.get("filament", ""))
            JOBS.append(jb)
            return ok(jb)
        if path == "api/queue" and m == "GET":
            vis = [j for j in JOBS if sees(ident, j)]
            return ok({"jobs": vis, "history": [j for j in HISTORY if sees(ident, j)], "printers": [{"serial": s} for s in visible(ident)]})
        r = re.fullmatch(r"api/queue/([0-9a-f]{12})/plate\.png", path)
        if r:
            return svg(plate_svg(r.group(1)))
        r = re.fullmatch(r"api/queue/([0-9a-f]{12})/(\w+)", path)
        if r and m == "POST":
            return ok(job_action(ident, r.group(1), r.group(2), J()))
        # slicing
        if path == "api/jobs" and m == "POST":
            name = (q.get("name") or [""])[0]
            if not re.search(r"\.(stl|3mf|step|stp|obj)$", name, re.I):
                raise Err(400, "Upload an .stl, .3mf, .obj or .step file.")
            jid = secrets.token_hex(6)
            SLICES[jid] = {"id": jid, "filename": name, "state": "uploaded"}
            return ok({"id": jid, "filename": name, "state": "uploaded"})
        r = re.fullmatch(r"api/jobs/([0-9a-f]{12})(?:/(\w+)(?:\.(\w+))?)?", path)
        if r and r.group(1) in SLICES:
            sl, what = SLICES[r.group(1)], r.group(2)
            if what is None and m == "GET":
                return ok({**sl, **make_job_state(sl)})
            if what == "slice" and m == "POST":
                b = J()
                if b.get("quality") not in QUALITY:
                    raise Err(400, "Unknown quality or plate.")
                MOCK_SLICES.append(b)
                sl.update(sliced_at=NOW(), quality=b["quality"], tray=b.get("tray"), bed=b.get("bed"), serial=b.get("serial"))
                return (202, {**sl, "state": "queued"}, "application/json")
            if what == "model" and m == "GET":
                return (200, b"solid x\nendsolid x\n", "application/octet-stream")
            if what == "plate" and r.group(3) == "png":
                return svg(plate_svg(sl["id"] + sl.get("quality", "")))
            if what == "plate" and r.group(3) == "gcode":
                return (200, gcode().encode(), "text/plain")
            if what == "queue" and m == "POST":
                b = J()
                if make_job_state(sl).get("state") != "done":
                    raise Err(409, "slice first")
                if not (admin or visible(ident)):
                    raise Err(403, "rule: adding a job needs an active grant on the target printer")
                if b.get("target") not in ("any", *PRINTERS) or (b["target"] == "any" and not b.get("model")):
                    raise Err(400, "model is required for target any")
                st = make_job_state(sl)
                jb = job(secrets.token_hex(6), re.sub(r"\.\w+$", "", sl["filename"]) + ".gcode.3mf", b["target"], uid, "queued",
                         model=b.get("model", ""), grams=st["info"]["grams"], age=0)
                jb["options"].update(b.get("options") or {})
                if b.get("name"):
                    jb["name"] = b["name"]
                MOCK_QUEUED.append({"options": b.get("options"), "name": b.get("name")})
                if uid == "u-jonas":
                    jb["state"] = "pending_approval"
                JOBS.append(jb)
                return ok(jb)
        if path == "api/_mock/spools-down":
            SPOOLS_DOWN["on"] = (q.get("on") or ["1"])[0] == "1"
            return ok({"on": SPOOLS_DOWN["on"]})
        if path.startswith("api/spools") and SPOOLS_DOWN["on"]:
            raise Err(502, "Bambu Cloud filament library: Bambu cloud answered HTTP 401 for /v1/design-user-service/my/filament/v2")
        if path == "api/load-requests" and m == "GET":
            return ok([r for r in LOAD_REQUESTS if admin or r["printer"] in USER_PRINTERS])
        r = re.fullmatch(r"api/load-requests/([0-9a-f]{12})", path)
        if r and m == "DELETE":
            req = next((x for x in LOAD_REQUESTS if x["id"] == r.group(1)), None)
            if not req:
                raise Err(404, "no such load request")
            if not admin and req["user_id"] != uid:
                raise Err(403, "rule: only the requester or an admin of the printer can cancel this request")
            LOAD_REQUESTS.remove(req)
            return ok({"removed": req["id"]})
        r = re.fullmatch(r"api/spools/(\d+)/load-request", path)
        if r and m == "POST":
            b = J()
            sp = next((s for s in SPOOLS if s["id"] == r.group(1)), None)
            if not sp:
                raise Err(404, "no such spool")
            if b.get("printer") not in PRINTERS or not (admin or b["printer"] in USER_PRINTERS):
                raise Err(403, "rule: you have no active grant on this printer")
            if not PRINTERS[b["printer"]]["ams"]:
                raise Err(400, "this printer has no AMS")
            old = next((x for x in LOAD_REQUESTS if x["spool_id"] == sp["id"] and x["printer"] == b["printer"]), None)
            if old:
                return ok(old)
            req = {"id": secrets.token_hex(6), "spool_id": sp["id"], "spool_label": f"{sp['brand']} {sp['type']} {sp['name']}", "colour": sp["colour"],
                   "printer": b["printer"], "user_id": uid, "user_name": IDENT[ident]["name"], "note": b.get("note") or "", "created_at": int(NOW())}
            LOAD_REQUESTS.append(req)
            return ok(req)
        if path == "api/spools":
            if m == "GET":
                return ok([dict(s, price_per_kg=PRICES.get(s["id"])) for s in SPOOLS])
            if not admin:
                raise Err(403, "rule: only admins of the spool's printer may change it")
            b = J(); price = b.pop("price_per_kg", None)
            sp = dict(cloud_spool("5" + secrets.token_hex(2), "", "", "", "", None, None), **b)
            SPOOLS.append(sp); PRICES[sp["id"]] = price
            return ok(dict(sp, price_per_kg=price))
        r = re.fullmatch(r"api/spools/(\d+)/prints", path)
        if r:
            its = []
            for it in history_out(ident, {})["items"]:
                used = [u for u in (it["job"] or {}).get("spools_used", []) if u["spool_id"] == r.group(1)]
                if used:
                    its.append(dict(it, spool_grams=sum(u["grams"] for u in used)))
            return ok({"items": its})
        r = re.fullmatch(r"api/spools/(\d+)", path)
        if r:
            sp = next((s for s in SPOOLS if s["id"] == r.group(1)), None)
            if not sp:
                raise Err(404, "no such spool")
            if not admin:
                raise Err(403, "rule: only admins of the spool's printer may change it")
            if m == "DELETE":
                SPOOLS.remove(sp)
                return ok({"removed": sp["id"]})
            b = J()
            if "price_per_kg" in b:
                PRICES[sp["id"]] = b.pop("price_per_kg")
            sp.update(b)
            return ok(dict(sp, price_per_kg=PRICES.get(sp["id"])))
        if path == "api/feedback" and m == "POST":
            b = J()
            if not isinstance(b.get("item"), str) or not b["item"] or not isinstance(b.get("description"), str) or not b["description"]:
                raise Err(400, "expected item and description")
            FEEDBACK.append(b)
            return ok({"ok": True})
        # admin-only
        if path.startswith("api/grants") or path.startswith("api/ha/") or path.startswith("api/setup") or path == "api/integration":
            need_admin()
        if path == "api/grants" and m == "GET":
            return ok(GRANTS)
        if path == "api/grants" and m == "POST":
            b = J()
            g = {"id": secrets.token_hex(6), "user_id": b["user_id"], "user_name": USERS.get(b["user_id"], b["user_id"]), "printer": b["printer"],
                 "role": b.get("role", "user"), "until": b.get("until"), "condition_entity": b.get("condition_entity"),
                 "needs_approval": bool(b.get("needs_approval")), "active": True}
            GRANTS.append(g)
            return ok(g)
        r = re.fullmatch(r"api/grants/(\w+)", path)
        if r and m == "DELETE":
            GRANTS[:] = [g for g in GRANTS if g["id"] != r.group(1)]
            return ok({})
        if path == "api/ha/switches" and m == "POST":
            b = J()
            who, pr = USERS.get(b.get("user_id")), PRINTERS.get(b.get("printer"))
            if not who or not (pr or b.get("printer") == "*"):
                raise Err(400, "unknown user or printer")
            name = "Druckwerk %s %s" % (who, pr["name"] if pr else "All printers")
            return ok({"entity_id": "input_boolean." + re.sub(r"[^a-z0-9]+", "_", name.lower()), "name": name, "state": "off"})
        if path == "api/history":
            return ok(history_out(ident, q))
        r = re.fullmatch(r"api/history/([\w-]+)/(settings|queue|file|gcode)", path)
        if r:
            item = next((i for i in history_out(ident, {})["items"] if str(i["id"]) == r.group(1)), None)
            if not item:
                raise Err(404, "unknown history item")
            out = settings_out(item)
            if r.group(2) == "settings":
                return ok(out)
            if r.group(2) == "gcode":
                if not out["preview"]:
                    raise Err(404, "no G-code for this print")
                return (200, gcode().encode(), "text/plain")
            if r.group(2) == "file":
                if not out["download"]:
                    raise Err(404, "no stored file for this print")
                return (200, b"solid mock\nendsolid mock\n", "application/octet-stream")
            if not out["can_reprint"]:
                raise Err(409, "No stored file for this print: " + out["reason"] + " Slice the model again.")
            old = item.get("job") or {}
            jb = job(secrets.token_hex(6), re.sub(r"\W+", "_", item["title"]) + ".gcode.3mf", item["serial"], uid, "queued", age=0, grams=item.get("grams"))
            jb["options"] = dict(old.get("options") or jb["options"])
            jb["name"] = old.get("name") or item["title"]
            MOCK_QUEUED.append({"options": jb["options"], "name": jb["name"], "target": jb["target"], "history": str(item["id"])})
            JOBS.append(jb)
            return ok(jb)
        r = re.fullmatch(r"api/history-cover/(\d+)\.svg", path)
        if r:
            if int(r.group(1)) in BROKEN_COVERS:
                return (200, b"this is not an image", "image/png")
            return svg(plate_svg(r.group(1)))
        if path == "api/ha/users":
            need_admin()
            return ok(HA_USERS)
        if path == "api/ha/entities":
            doms = (q.get("domains") or [""])[0].split(",")
            return ok([e for e in ENTITIES if e["entity_id"].split(".")[0] in doms])
        if path == "api/integration":
            return ok({"url": "http://local-druckwerk:8099", "secret": "mock-secret-" + "0" * 20, "connected": False})
        if path == "api/setup" and m == "GET":
            return ok({"accounts": SETUP["accounts"], "printers": list(SETUP["printers"].values())})
        if path == "api/setup/login/start":
            b = J(); em = b.get("email", "")
            if not em or not b.get("password"):
                raise Err(400, "email and password are required")
            if "code" in em:
                return ok({"state": "need_code", "account": em})
            if "tfa" in em:
                return ok({"state": "need_tfa", "account": em})
            SETUP["accounts"].append({"id": secrets.token_hex(3), "email": em, "logged_in": True, "daemon_running": True})
            return ok({"state": "done", "account": em})
        if path == "api/setup/login/code":
            b = J()
            SETUP["accounts"].append({"id": secrets.token_hex(3), "email": b.get("email"), "logged_in": True, "daemon_running": True})
            return ok({"state": "done", "account": b.get("email")})
        r = re.fullmatch(r"api/setup/accounts/(\w+)", path)
        if r and m == "DELETE":
            SETUP["accounts"][:] = [a for a in SETUP["accounts"] if a["id"] != r.group(1)]
            return ok({})
        r = re.fullmatch(r"api/setup/printers/([^/]+)", path)
        if r and r.group(1) in SETUP["printers"]:
            p = SETUP["printers"][r.group(1)]
            if m == "PUT":
                p["configured"] = True; p["config"] = J(); OWNERS[r.group(1)] = p["config"].get("owner") or None
            else:
                p["configured"] = False
            return ok(p)
        raise Err(404, "no route " + m + " " + path)

    def do_GET(self): self.handle_any("GET")
    def do_POST(self): self.handle_any("POST")
    def do_PUT(self): self.handle_any("PUT")
    def do_DELETE(self): self.handle_any("DELETE")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    print(f"mock hub on http://127.0.0.1:{port}/?as=admin  (also user, admin-new)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
