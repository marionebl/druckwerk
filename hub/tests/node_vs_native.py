#!/usr/bin/env python3
"""PROTOTYPE (measurement only): compares the G-code of the Node/WASM runner (hub/slicer/engine/node_slice.mjs) with the
G-code of native Bambu Studio for the same plate. Reads the numbers the browser CI comparison uses (layers, print time,
filament grams, bounding box, header and key settings, G-code guard).

usage: node_vs_native.py <model H2S|P1S> <native .gcode.3mf|.gcode> <node .gcode.3mf|.gcode> [json out]
Exit 0 always; the verdict lines say PASS or FAIL per check."""
import difflib
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))


def read_gcode(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            return z.read("Metadata/plate_1.gcode").decode("utf-8", "replace")
    return open(path, encoding="utf-8", errors="replace").read()


def secs(text):
    t = 0
    for n, u in re.findall(r"(\d+)\s*([dhms])", text):
        t += int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u]
    return t


def analyse(text):
    lines = text.splitlines()
    r = {"lines": len(lines)}
    m = re.search(r"^; total layer number:\s*(\d+)", text, re.M)
    r["layers"] = int(m.group(1)) if m else None
    m = re.search(r"total estimated time:\s*([^\n;]+)", text)
    r["time_s"] = secs(m.group(1)) if m else None
    m = re.search(r"^; filament used \[g\]\s*=\s*([\d.,]+)", text, re.M) or re.search(r"^; total filament weight \[g\]\s*:\s*([\d.]+)", text, re.M)
    r["grams"] = float(m.group(1).split(",")[0]) if m else None
    m = re.search(r"^; filament used \[mm\]\s*=\s*([\d.,]+)", text, re.M) or re.search(r"^; total filament length \[mm\]\s*:\s*([\d.]+)", text, re.M)
    r["filament_mm"] = float(m.group(1).split(",")[0]) if m else None
    m = re.search(r"^; max_z_height:\s*([\d.]+)", text, re.M)
    r["max_z"] = float(m.group(1)) if m else None
    cfg = {}
    a = text.find("; CONFIG_BLOCK_START")
    b = text.find("; CONFIG_BLOCK_END")
    for line in text[a:b].splitlines() if a >= 0 else []:
        m = re.match(r"^; ([A-Za-z0-9_]+) = (.*)$", line)
        if m:
            cfg[m.group(1)] = m.group(2)
    r["config"] = cfg
    # Motion pass: bounding box of extruding moves after the first layer change, temperatures, feedrates, retractions.
    x = y = z = e = 0.0
    rel_e = False
    started = False
    lo = [1e9, 1e9, 1e9]
    hi = [-1e9, -1e9, -1e9]
    temps, maxf, retr, ext_moves, layer_changes = set(), 0.0, 0, 0, 0
    feat, fbox = None, {}
    start_end = end_start = None
    for i, line in enumerate(lines):
        line = line.strip()
        fm = re.match(r"^;\s*(?:FEATURE|TYPE):\s*(.+)$", line)
        if fm:
            feat = fm.group(1).strip()
        if line.startswith(";LAYER_CHANGE") or line.startswith("; CHANGE_LAYER"):
            layer_changes += 1
            if not started:
                started, start_end = True, i
            continue
        if started and end_start is None and "printer finish" in line:
            end_start = i
        if line.startswith("M82"):
            rel_e = False
        elif line.startswith("M83"):
            rel_e = True
        mm = re.match(r"^(M104|M109|M140|M190|M141)\b.*?\bS(\d+)", line)
        if mm:
            temps.add((mm.group(1), int(mm.group(2))))
        if not re.match(r"^G[0-3]\b", line):
            continue
        f = re.search(r"\bF(\d*\.?\d+)", line)
        if f and started:
            maxf = max(maxf, float(f.group(1)))
        nx, ny, nz, ne = (re.search(r"\b%s(-?\d*\.?\d+)" % a, line) for a in "XYZE")
        px, py = x, y
        if nx:
            x = float(nx.group(1))
        if ny:
            y = float(ny.group(1))
        if nz:
            z = float(nz.group(1))
        d = 0.0
        if ne:
            v = float(ne.group(1))
            d = v if rel_e else v - e
            e = e + v if rel_e else v
        if started and end_start is None and d > 0 and (nx or ny):
            ext_moves += 1
            for k, v in ((0, x), (1, y), (2, z)):
                lo[k] = min(lo[k], v)
                hi[k] = max(hi[k], v)
            b = fbox.setdefault(feat or "?", [1e9, 1e9, -1e9, -1e9])
            b[0], b[1], b[2], b[3] = min(b[0], x), min(b[1], y), max(b[2], x), max(b[3], y)
        if started and d < 0:
            retr += 1
    r["bbox"] = {"min": [round(v, 2) for v in lo], "max": [round(v, 2) for v in hi]}
    r["feature_bbox"] = {k: [round(v, 1) for v in b] for k, b in fbox.items()}
    mb = [b for k, b in fbox.items() if k.lower() not in ("brim", "skirt", "prime tower", "wipe tower")]
    r["model_bbox_xy"] = [round(f(b[i] for b in mb), 2) for i, f in ((0, min), (1, min), (2, max), (3, max))] if mb else None
    r["temps"] = sorted(temps)
    r["max_feed_mm_min"] = maxf
    r["retractions"] = retr
    r["extrusion_moves"] = ext_moves
    r["layer_changes"] = layer_changes

    def code(seg):
        return [re.sub(r"\s*;.*$", "", s).strip() for s in seg if re.sub(r"\s*;.*$", "", s).strip()]
    r["start_code"] = code(lines[: start_end or 0][lines.index("; EXECUTABLE_BLOCK_START") + 1:] if "; EXECUTABLE_BLOCK_START" in lines else lines[: start_end or 0])
    r["end_code"] = code(lines[end_start:] if end_start else [])
    return r


KEYS = ["layer_height", "initial_layer_print_height", "nozzle_diameter", "nozzle_temperature", "nozzle_temperature_initial_layer",
        "textured_plate_temp", "textured_plate_temp_initial_layer", "hot_plate_temp", "hot_plate_temp_initial_layer", "outer_wall_speed",
        "inner_wall_speed", "sparse_infill_speed", "internal_solid_infill_speed", "top_surface_speed", "initial_layer_speed", "travel_speed",
        "default_acceleration", "outer_wall_acceleration", "initial_layer_acceleration", "filament_flow_ratio", "pressure_advance",
        "enable_pressure_advance", "retraction_length", "retraction_speed", "z_hop", "wall_loops", "sparse_infill_density",
        "sparse_infill_pattern", "fan_min_speed", "fan_max_speed", "close_fan_the_first_x_layers", "filament_max_volumetric_speed",
        "filament_type", "filament_density", "seam_position", "brim_type", "enable_support", "bottom_shell_layers", "top_shell_layers",
        "printer_model", "curr_bed_type", "filament_settings_id", "print_settings_id", "printer_settings_id"]


def main():
    model, nat_p, node_p = sys.argv[1:4]
    out = {}
    nat, node = analyse(read_gcode(nat_p)), analyse(read_gcode(node_p))
    out["native"] = {k: v for k, v in nat.items() if k not in ("config", "start_code", "end_code")}
    out["node"] = {k: v for k, v in node.items() if k not in ("config", "start_code", "end_code")}
    fails = []

    def pct(a, b):
        return None if not a or b is None else 100.0 * (b - a) / a
    print(f"== {model}: native Bambu Studio vs Node WASM (Orca engine)")
    for k in ("layers", "time_s", "grams", "filament_mm", "max_z", "lines", "extrusion_moves", "retractions", "max_feed_mm_min"):
        a, b = nat[k], node[k]
        p = pct(a, b)
        print(f"  {k:16s} native={a} node={b}" + (f" diff={p:+.2f}%" if p is not None else ""))
        out.setdefault("diff_pct", {})[k] = p
    print(f"  bbox of all extrusion (brim and skirt included): native={nat['bbox']} node={node['bbox']}")
    print(f"  bbox X/Y of the model only (no brim, skirt, tower): native={nat['model_bbox_xy']} node={node['model_bbox_xy']}")
    print(f"  bbox X/Y per feature, native: Brim={nat['feature_bbox'].get('Brim')} Skirt={nat['feature_bbox'].get('Skirt')} | node: Brim={node['feature_bbox'].get('Brim')} Skirt={node['feature_bbox'].get('Skirt')}")
    print(f"  temps native={nat['temps']} node={node['temps']}")
    if nat["layers"] and node["layers"]:
        d = abs(node["layers"] - nat["layers"]) / nat["layers"] * 100
        print(f"CHECK layer count within 2%: {'PASS' if d <= 2 else 'FAIL'} ({node['layers']} vs {nat['layers']}, {d:.2f}%)")
        if d > 2:
            fails.append("layers")
    else:
        print("CHECK layer count within 2%: FAIL (missing)")
        fails.append("layers")
    nb, bb = nat["bbox"], node["bbox"]
    dd = max(abs(a - b) for a, b in zip(nb["min"][:2] + nb["max"][:2], bb["min"][:2] + bb["max"][:2]))
    print(f"CHECK bbox X/Y of all extrusion within 2 mm: {'PASS' if dd <= 2 else 'FAIL'} (max corner difference {dd:.2f} mm)")
    if dd > 2:
        fails.append("bbox-all")
    nm, om = nat["model_bbox_xy"], node["model_bbox_xy"]
    dm = max(abs(a - b) for a, b in zip(nm, om)) if nm and om else 999
    print(f"CHECK bbox X/Y of the model only within 2 mm: {'PASS' if dm <= 2 else 'FAIL'} (max corner difference {dm:.2f} mm)")
    if dm > 2:
        fails.append("bbox-model")
    bed = {"H2S": (340, 320), "P1S": (256, 256)}[model]
    out_of_bed = [name for name, bx in (("native", nb), ("node", bb)) if bx["min"][0] < 0 or bx["min"][1] < 0 or bx["max"][0] > bed[0] or bx["max"][1] > bed[1]]
    print(f"CHECK all extrusion inside the {bed[0]}x{bed[1]} bed: {'PASS' if not out_of_bed else 'FAIL for ' + ','.join(out_of_bed)}")
    if "node" in out_of_bed:
        fails.append("node-outside-bed")
    # settings
    nc, oc = nat["config"], node["config"]
    common = sorted(set(nc) & set(oc))
    diff = [k for k in common if nc[k] != oc[k]]
    print(f"  config keys: native={len(nc)} node={len(oc)} common={len(common)} differing={len(diff)} only-native={len(set(nc) - set(oc))} only-node={len(set(oc) - set(nc))}")
    print("  key settings (native | node):")
    for k in KEYS:
        if k in nc or k in oc:
            same = "same" if nc.get(k) == oc.get(k) else "DIFF"
            print(f"    {same} {k}: {str(nc.get(k))[:70]!r} | {str(oc.get(k))[:70]!r}")
    print("  all differing common keys (first 200):")
    for k in diff[:200]:
        print(f"    {k}: {nc[k][:90]!r} | {oc[k][:90]!r}")
    print("  keys only in native:", sorted(set(nc) - set(oc))[:80])
    print("  keys only in node:", sorted(set(oc) - set(nc))[:80])
    out["config_diff"] = {k: [nc[k], oc[k]] for k in diff}
    # executed start/end code
    for part in ("start_code", "end_code"):
        a, b = nat[part], node[part]
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        print(f"  {part}: native {len(a)} lines, node {len(b)} lines, similarity {sm.ratio():.3f}")
        shown = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal" and shown < 40:
                print(f"    {tag}: native {a[i1:i2][:4]} | node {b[j1:j2][:4]}")
                shown += 1
    # guard
    try:
        import gcode_guard
        for name, p in (("native", nat_p), ("node", node_p)):
            if p.endswith(".3mf"):
                probs = gcode_guard.check_gcode_3mf(p, model, use_ams=True)
                print(f"GUARD {name}: {'PASS' if not probs else 'FAIL ' + str(probs)}")
                out.setdefault("guard", {})[name] = probs
                if probs and name == "node":
                    fails.append("guard")
            else:
                print(f"GUARD {name}: skipped (not a .3mf)")
    except Exception as e:  # noqa: BLE001
        print("GUARD error", e)
    print("OVERALL:", "FAIL " + ",".join(fails) if fails else "PASS")
    if len(sys.argv) > 4:
        json.dump(out, open(sys.argv[4], "w"), indent=1)


main()
