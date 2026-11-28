"""The settings a sliced .gcode.3mf was made with: Metadata/project_settings.config (JSON) and slice_info.config (XML).

read(path) is pure file parsing, so the tests need no server. Values of per-filament keys are taken for the filaments
that the plate uses; other list values (one entry per nozzle variant) are shown once when all entries are equal.
"""
import json
import re
import xml.etree.ElementTree as ET
import zipfile

import files

# (key, label, unit). The curated view; the full list is "all".
GROUPS = [
    ("Quality", [("layer_height", "Layer height", "mm"), ("initial_layer_print_height", "First layer height", "mm"),
                 ("line_width", "Line width", "mm"), ("initial_layer_line_width", "First layer line width", "mm"),
                 ("outer_wall_line_width", "Outer wall line width", "mm"), ("inner_wall_line_width", "Inner wall line width", "mm"),
                 ("top_surface_line_width", "Top surface line width", "mm"), ("sparse_infill_line_width", "Infill line width", "mm"),
                 ("seam_position", "Seam position", ""), ("ironing_type", "Ironing", ""), ("wall_generator", "Wall generator", "")]),
    ("Strength", [("wall_loops", "Wall loops", ""), ("top_shell_layers", "Top shell layers", ""),
                  ("bottom_shell_layers", "Bottom shell layers", ""), ("top_shell_thickness", "Top shell thickness", "mm"),
                  ("bottom_shell_thickness", "Bottom shell thickness", "mm"), ("sparse_infill_density", "Infill density", ""),
                  ("sparse_infill_pattern", "Infill pattern", ""), ("top_surface_pattern", "Top surface pattern", "")]),
    ("Support and adhesion", [("enable_support", "Support", ""), ("support_type", "Support type", ""), ("support_style", "Support style", ""),
                              ("support_threshold_angle", "Support threshold angle", "°"),
                              ("support_on_build_plate_only", "Support on build plate only", ""), ("brim_type", "Brim", ""),
                              ("skirt_loops", "Skirt loops", ""), ("raft_layers", "Raft layers", ""), ("enable_prime_tower", "Prime tower", "")]),
    ("Speed", [("initial_layer_speed", "First layer", "mm/s"), ("outer_wall_speed", "Outer wall", "mm/s"),
               ("inner_wall_speed", "Inner wall", "mm/s"), ("sparse_infill_speed", "Infill", "mm/s"),
               ("top_surface_speed", "Top surface", "mm/s"), ("bridge_speed", "Bridge", "mm/s"), ("support_speed", "Support", "mm/s"),
               ("travel_speed", "Travel", "mm/s"), ("default_acceleration", "Acceleration", "mm/s²")]),
    ("Filament", [("filament_settings_id", "Preset", ""), ("filament_type", "Type", ""), ("filament_vendor", "Brand", ""),
                  ("nozzle_temperature", "Nozzle temperature", "°C"), ("nozzle_temperature_initial_layer", "First layer nozzle temperature", "°C"),
                  ("filament_flow_ratio", "Flow ratio", ""), ("filament_max_volumetric_speed", "Max volumetric speed", "mm³/s"),
                  ("pressure_advance", "Pressure advance", "")]),
    ("Plate and bed", [("curr_bed_type", "Build plate", ""), ("@bed_temp", "Bed temperature", "°C"),
                       ("@bed_temp_initial", "First layer bed temperature", "°C")]),
    ("Printer", [("printer_model", "Printer model", ""), ("printer_settings_id", "Printer preset", ""), ("nozzle_diameter", "Nozzle diameter", "mm"),
                 ("printer_variant", "Nozzle variant", ""), ("print_settings_id", "Process preset", ""), ("print_sequence", "Print sequence", "")]),
]
PER_FILAMENT = {"filament_settings_id", "filament_type", "filament_vendor", "nozzle_temperature", "nozzle_temperature_initial_layer",
                "filament_flow_ratio", "filament_max_volumetric_speed", "pressure_advance"}
# Build plate name -> the key family of its temperatures.
BED_KEYS = {"Cool Plate": "cool_plate", "Engineering Plate": "eng_plate", "High Temp Plate": "hot_plate",
            "Textured PEI Plate": "textured_plate", "Supertack Plate": "supertack_plate", "Textured Cool Plate": "cool_plate"}
BOOLEAN = {"enable_support", "support_on_build_plate_only", "enable_prime_tower"}
HIDDEN = re.compile(r"password|token|secret|serial|email|access_code|user", re.I)
LONG = 300


def text(v):
    if isinstance(v, bool):
        return "On" if v else "Off"
    return str(v)


def joined(v, keep=None):
    """A list value as one text: the chosen entries, equal entries once."""
    if not isinstance(v, list):
        return text(v)
    items = [text(x) for i, x in enumerate(v) if keep is None or i in keep] or [text(x) for x in v]
    uniq = list(dict.fromkeys(items))
    return uniq[0] if len(uniq) == 1 and keep is None else " / ".join(items if keep is not None else uniq)


def info(z):
    """(plate facts, used filament slots) from slice_info.config; empty when absent."""
    try:
        root = ET.fromstring(files.read_member(z, "Metadata/slice_info.config", files.CONFIG_MAX))
    except (KeyError, ET.ParseError):
        return {}, []
    plate = {m.get("key"): m.get("value") for m in root.iterfind("./plate/metadata")}
    slots = [int(f.get("id")) - 1 for f in root.iterfind("./plate/filament") if (f.get("id") or "").isdigit()]
    plate["slicer"] = next((i.get("value") for i in root.iterfind("./header/header_item") if i.get("key") == "X-BBL-Client-Version"), None)
    plate["objects"] = len(root.findall("./plate/object"))
    return plate, slots


def value(s, key, unit, slots):
    if key.startswith("@"):
        fam = BED_KEYS.get(s.get("curr_bed_type"))
        v = s.get(fam + ("_temp_initial_layer" if key == "@bed_temp_initial" else "_temp")) if fam else None
        return None if v is None else joined(v)
    v = s.get(key)
    if v is None or v == "":
        return None
    if key in BOOLEAN:
        v = text(v) if isinstance(v, bool) else ("On" if str(v) == "1" else "Off")
    return joined(v, set(slots) if key in PER_FILAMENT and slots else None)


def read(path):
    """{slicer, groups: [{title, rows: [{key, label, value, unit}]}], all: [[key, text]], plate} or None without settings."""
    try:
        with zipfile.ZipFile(path) as z:
            s = json.loads(files.read_member(z, "Metadata/project_settings.config", files.CONFIG_MAX))
            plate, slots = info(z)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
    if not isinstance(s, dict):
        return None
    groups = []
    for title, rows in GROUPS:
        got = [{"key": k, "label": label, "value": value(s, k, unit, slots), "unit": unit} for k, label, unit in rows]
        got = [r for r in got if r["value"] is not None]
        if got:
            groups.append({"title": title, "rows": got})
    every = [[k, joined(v)[:LONG] + ("…" if len(joined(v)) > LONG else "")] for k, v in sorted(s.items()) if not HIDDEN.search(k)]
    return {"slicer": plate.pop("slicer", None) or s.get("version"), "groups": groups, "all": every, "plate": plate}
