"""Printer state from the bridge's MQTT status (the `print` object of GET /api/status/<serial>), in the shapes the app serves.

The field meanings follow the open-source ha-bambulab library (MIT, github.com/greghesp/ha-bambulab, pybambu/models.py and
const.py): only the mappings the app needs, not its text tables. `view()` is pure, so tests cover it without a server.
"""
import datetime
import math

GCODE_STATES = {"failed", "finish", "idle", "init", "offline", "pause", "prepare", "running", "slicing"}
ACTIVE_STATES = ("RUNNING", "PAUSE", "PREPARE")  # a finish time only means something then
# Printer stage ids 0 to 35 (the pre-print and pause stages); -1 and 255 mean idle. Other ids are reported as unknown.
STAGES = ("printing", "auto_bed_leveling", "heatbed_preheating", "sweeping_xy_mech_mode", "changing_filament", "m400_pause",
          "paused_filament_runout", "heating_hotend", "calibrating_extrusion", "scanning_bed_surface", "inspecting_first_layer",
          "identifying_build_plate_type", "calibrating_micro_lidar", "homing_toolhead", "cleaning_nozzle_tip",
          "checking_extruder_temperature", "paused_user", "paused_front_cover_falling", "calibrating_micro_lidar",
          "calibrating_extrusion_flow", "paused_nozzle_temperature_malfunction", "paused_heat_bed_temperature_malfunction",
          "filament_unloading", "paused_skipped_step", "filament_loading", "calibrating_motor_noise", "paused_ams_lost",
          "paused_low_fan_speed_heat_break", "paused_chamber_temperature_control_error", "cooling_chamber", "paused_user_gcode",
          "motor_noise_showoff", "paused_nozzle_filament_covered_detected", "paused_cutter_error", "paused_first_layer_error",
          "paused_nozzle_clog")
SEVERITY = {1: "fatal", 2: "serious", 3: "common", 4: "info"}  # high word of an HMS code
HMS_NOT_AN_ERROR = {"0C00_0300_0003_000B"}  # "inspecting the first layer"
HMS_WIKI = "https://wiki.bambulab.com/en/hms/home"
EXTERNAL_SLOTS = ("255", "254")  # vir_slot ids of the external spools
UNIT_IDS = tuple(range(0, 4)) + tuple(range(128, 136))  # AMS and AMS HT; rack units have no slot numbering we use
SPOOL, STEADY, LEGACY_MAX = 0x01, 0x08, 3  # tray `state` bit flags


def num(v, cast=float):
    try:
        return cast(float(v))
    except (TypeError, ValueError, OverflowError):
        return None


def _int(v):
    try:
        return None if isinstance(v, bool) else int(v)
    except (TypeError, ValueError):
        return None


def _word(v):
    """A packed temperature: low word the current value, high word the target."""
    n = _int(v)
    return None if n is None else (n & 0xFFFF, n >> 16 & 0xFFFF)


def _dig(d, *keys):
    for k in keys:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def _colour(v):
    return "".join(c for c in str(v or "") if c in "0123456789abcdefABCDEF")[:6].upper()


def _spool_in(state):
    """A tray `state` says a spool sits in the slot and nothing moves."""
    if not state & SPOOL:
        return False
    return state == LEGACY_MAX if state <= LEGACY_MAX else bool(state & STEADY)


def tray(t):
    """One tray (or external spool) entry as {slot, type, colour, name, remain_pct, uid}; type is "" when empty."""
    state = _int(t.get("state")) if "state" in t else None
    kind = str(t.get("tray_type") or "")
    loaded = _spool_in(state) if state is not None else bool(t.get("tray_info_idx") or kind)
    remain = num(t.get("remain"), int)
    uid = next((str(x) for x in (t.get("tag_uid"), t.get("tray_uuid")) if x and set(str(x)) - {"0"}), "")
    return {"slot": _int(t.get("id")), "type": kind if loaded else "", "colour": _colour(t.get("tray_color")) if loaded else "",
            "name": (str(t.get("tray_sub_brands") or kind) or "?") if loaded else "",
            "remain_pct": remain if loaded and remain is not None and remain >= 0 else None, "uid": uid}


def ams_units(p):
    """[{unit, humidity_index, temp, trays}] of the AMS units in a status."""
    out = []
    for u in _dig(p, "ams", "ams") or []:
        unit = _int(u.get("id")) if isinstance(u, dict) else None
        if unit not in UNIT_IDS:
            continue
        hum, temp = _int(u.get("humidity")), num(u.get("temp"))
        out.append({"unit": unit, "humidity_index": 6 - hum if hum and 1 <= hum <= 5 else None,  # the integration shows 1 (dry) .. 5 (wet)
                    "temp": temp,
                    "trays": sorted((tray(t) for t in u.get("tray") or [] if isinstance(t, dict) and _int(t.get("id")) is not None),
                                    key=lambda t: t["slot"])})
    return sorted(out, key=lambda a: a["unit"])


def external(p):
    """The spool on an external holder, if one is loaded."""
    slots = {str(s.get("id")): s for s in p.get("vir_slot") or [] if isinstance(s, dict)}
    for sid in EXTERNAL_SLOTS:
        t = tray(slots[sid]) if sid in slots else None
        if t and t["type"]:
            return {"type": t["type"], "name": t["name"], "colour": t["colour"]}
    return None


def _nozzle(p):
    """(current, target) of the active nozzle."""
    info = _dig(p, "device", "extruder", "info")
    if isinstance(info, list):
        state = _int(_dig(p, "device", "extruder", "state"))
        active = state >> 4 & 0xF if state is not None else 0
        entry = next((e for e in info if isinstance(e, dict) and _int(e.get("id")) == active), None)
        if entry and _word(entry.get("temp")):
            return _word(entry["temp"])
    cur, tgt = num(p.get("nozzle_temper")), num(p.get("nozzle_target_temper"))
    return (None if cur is None else round(cur)), (None if tgt is None else round(tgt))


def _bed(p):
    packed = _word(_dig(p, "device", "bed", "info", "temp"))
    if packed:
        return packed
    cur, tgt = num(p.get("bed_temper")), num(p.get("bed_target_temper"))
    return (None if cur is None else round(cur)), (None if tgt is None else math.floor(tgt))


def _chamber(p):
    packed = _word(_dig(p, "device", "ctc", "info", "temp"))
    if packed:
        return packed[0]
    cur = num(p.get("chamber_temper"))
    return None if cur is None else round(cur)


def _snow(p):
    """(unit, slot) from the extruder's `snow` (unit << 8 | slot), else from the legacy tray_now; None when unknown."""
    info = _dig(p, "device", "extruder", "info")
    state = _int(_dig(p, "device", "extruder", "state"))
    active = state >> 4 & 0xF if state is not None else 0
    entry = next((e for e in info if isinstance(e, dict) and _int(e.get("id")) == active), {}) if isinstance(info, list) else {}
    if _int(entry.get("snow")) is not None:
        v = _int(entry["snow"])
        return (255, 255) if v == 0xFF else (v >> 8, v & 0xFF)
    now = _int(_dig(p, "ams", "tray_now"))
    if now is None or now == 255:
        return 255, 255
    if now == 254:
        return 255, 0  # the external spool
    return (now, 0) if now >= 128 else (now >> 2, now & 3)


def active_tray(p, units, ext):
    """{unit, slot, name} of the loaded filament, None for none. An external spool is unit 255, slot 0."""
    unit, slot = _snow(p)
    if slot == 255:
        return None
    if unit in (254, 255):
        return {"unit": 255, "slot": slot, "name": (ext or {}).get("name") or "?"} if ext else None
    t = next((t for u in units if u["unit"] == unit for t in u["trays"] if t["slot"] == slot), None)
    return {"unit": unit, "slot": slot, "name": (t or {}).get("name") or "?"} if t and t["type"] else None


def hms(p):
    out = []
    for e in p.get("hms") or []:
        attr, code = _int(e.get("attr")), _int(e.get("code"))
        if not attr or not code or attr < 0 or code < 0:
            continue
        text = "%04X_%04X_%04X_%04X" % (attr >> 16, attr & 0xFFFF, code >> 16, code & 0xFFFF)
        if text not in HMS_NOT_AN_ERROR:
            out.append({"code": "HMS_" + text, "severity": SEVERITY.get(code >> 16, "info"), "text": "", "url": HMS_WIKI})
    return out


def stage(p):
    state = str(p.get("gcode_state") or "")
    sid = _int(p.get("stg_cur"))
    if sid is None or state == "FAILED" or (p.get("print_type") == "idle" and sid == 0):
        sid = 255  # after a cancelled print the printer keeps reporting its last stage
    return "idle" if sid in (-1, 255) else STAGES[sid] if 0 <= sid < len(STAGES) else None


def end_time(p, now):
    left = num(p.get("mc_remaining_time"), int)
    if left is None or p.get("gcode_state") not in ACTIVE_STATES:
        return None
    at = datetime.datetime.fromtimestamp(now + left * 60, datetime.timezone.utc).replace(second=0, microsecond=0)
    return at.isoformat()


def view(p, now):
    """Everything the app shows of one printer, from one `print` object. `p` empty means the bridge could not say."""
    if not p:
        return None
    units, ext = ams_units(p), external(p)
    state = str(p.get("gcode_state") or "").lower()
    nozzle, bed = _nozzle(p), _bed(p)
    return {
        "status": state if state in GCODE_STATES else None, "stage": stage(p),
        "progress": num(p.get("mc_percent")), "remaining_min": num(p.get("mc_remaining_time"), int), "end_time": end_time(p, now),
        "layer": num(p.get("layer_num"), int), "total_layers": num(p.get("total_layer_num"), int),
        "nozzle_temp": nozzle[0], "nozzle_target": nozzle[1], "bed_temp": bed[0], "bed_target": bed[1], "chamber_temp": _chamber(p),
        "task_name": str(p.get("subtask_name") or "") or None,
        "active_tray": active_tray(p, units, ext), "ams": units, "external": ext, "hms": hms(p),
        "lights": {str(x.get("node")): x.get("mode") for x in p.get("lights_report") or [] if isinstance(x, dict)},
    }
