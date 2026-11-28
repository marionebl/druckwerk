"""Printer registry: printers keyed by serial, stored as JSON in the data dir (no serials in code)."""
import json
import os
import re
import tempfile
import threading

NOZZLE_SIZES = ("0.2", "0.4", "0.6", "0.8")  # mm, the sizes Bambu printers take
FIELDS = ("name", "model", "account", "prefix", "plate_type", "nozzles", "pause_entities", "electricity_kwh_price", "owner")  # owner: HA user id or ""


def check_nozzles(nozzles):
    """The library as a list of {"diameter", "type"} without repeats; ValueError on anything else."""
    out = []
    for n in nozzles or []:
        d, t = (n.get("diameter"), n.get("type") or "") if isinstance(n, dict) else (None, "")
        if d not in NOZZLE_SIZES or not isinstance(t, str) or not re.fullmatch(r"[a-z_]{0,30}", t):
            raise ValueError("nozzles must be a list of {diameter: %s, type?: e.g. hardened_steel}" % " | ".join(NOZZLE_SIZES))
        if not any(o["diameter"] == d and o["type"] == t for o in out):
            out.append({"diameter": d, "type": t})
    return out


def write_json(path, data):
    # Write-then-rename so a crash never leaves a half-written queue file.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


class Registry:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "printers.json")
        self.lock = threading.RLock()
        os.makedirs(data_dir, exist_ok=True)
        self.printers = {}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.printers = json.load(f)
            old = [p for p in self.printers.values() if "nozzle" in p]
            for p in old:  # the single nozzle of earlier versions becomes the library's first entry
                p["nozzles"] = p.get("nozzles") or ([{"diameter": p["nozzle"], "type": ""}] if p["nozzle"] in NOZZLE_SIZES else [])
                del p["nozzle"]
            if old:
                write_json(self.path, self.printers)
        # Serials an admin removed: auto-registration skips them. Own file, so printers.json keeps its format.
        self.removed_path = os.path.join(data_dir, "removed_printers.json")
        self.removed = []
        if os.path.exists(self.removed_path):
            with open(self.removed_path) as f:
                self.removed = json.load(f)

    def put(self, serial, **fields):
        serial = str(serial or "").strip()
        if not serial:
            raise ValueError("serial is required")
        unknown = set(fields) - set(FIELDS)
        if unknown:
            raise ValueError("unknown printer fields: " + ", ".join(sorted(unknown)))
        for key in ("name", "model", "prefix"):
            if not str(fields.get(key) or "").strip():
                raise ValueError(key + " is required")
        pause = fields.get("pause_entities") or []
        if not isinstance(pause, list) or not all(isinstance(e, str) and "." in e for e in pause):
            raise ValueError("pause_entities must be a list of entity ids")
        entry = {k: fields.get(k) or "" for k in FIELDS}
        entry["pause_entities"] = pause
        entry["nozzles"] = check_nozzles(fields.get("nozzles"))
        with self.lock:
            self.printers[serial] = entry
            write_json(self.path, self.printers)
            if serial in self.removed:
                self.removed.remove(serial)
                write_json(self.removed_path, self.removed)
        return dict(entry, serial=serial)

    def set_owner(self, serial, user_id):
        """The owner (an HA user id, "" for none) administers this printer. Ownership belongs to the printer, not to a grant."""
        with self.lock:
            if serial not in self.printers:
                raise ValueError("unknown printer")
            self.printers[serial]["owner"] = str(user_id or "")
            write_json(self.path, self.printers)

    def remove(self, serial):
        with self.lock:
            self.printers.pop(serial, None)
            write_json(self.path, self.printers)

    def mark_removed(self, serial):
        with self.lock:
            if serial not in self.removed:
                self.removed.append(serial)
                write_json(self.removed_path, self.removed)

    def is_removed(self, serial):
        return serial in self.removed

    def get(self, serial):
        entry = self.printers.get(serial)
        return dict(entry, serial=serial) if entry else None

    def all(self):
        return [dict(v, serial=k) for k, v in self.printers.items()]

    def entities(self, serial):
        """vendor-integration sensor ids for a printer, from its entity prefix."""
        p = self.printers[serial]["prefix"]
        return {key: "sensor.%s_%s" % (p, key) for key in
                ("print_status", "active_tray", "current_stage", "print_progress")}
