"""Filament grams from a slice, cost arithmetic, spool matching and the price store."""
import os
import re
import threading
import xml.etree.ElementTree as ET
import zipfile

import files
from queue_registry import read_json, write_json

# Average printing watts, PLA column of the vendor wiki's "Supply voltage" page;
# H2S has no published figure, 150 is our assumption between A1 (95) and H2D (197).
WATTS = {"H2S": 150, "X1C": 105, "X1": 105, "X1E": 105, "P1S": 105, "P1P": 110, "A1": 95, "A1 MINI": 80}
DEFAULT_WATTS = 105


def slice_usage(path):
    """{"seconds", "filaments": [{"type", "colour", "grams"}], "grams"} from Metadata/slice_info.config of a .gcode.3mf."""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(files.read_member(z, "Metadata/slice_info.config", files.CONFIG_MAX))
    plate = root.find("plate")
    meta = {m.get("key"): m.get("value") for m in plate.iter("metadata")}
    fils = [{"type": f.get("type") or "", "colour": re.sub(r"[^0-9A-Fa-f]", "", f.get("color") or "")[:6].upper(),
             "grams": float(f.get("used_g") or 0)} for f in plate.iter("filament")]
    return {"seconds": int(float(meta.get("prediction") or 0)), "filaments": fils,
            "grams": round(sum(f["grams"] for f in fils), 2)}


def watts(model):
    return WATTS.get(str(model or "").upper(), DEFAULT_WATTS)


def job_cost(filament_grams, price_per_kg, seconds, model, kwh_price):
    """Filament (grams x price per kg / 1000, per filament) plus electricity (hours x watts / 1000 x kWh price)."""
    material = sum(g * p / 1000 for g, p in zip(filament_grams, price_per_kg))
    power = seconds / 3600 * watts(model) / 1000 * (kwh_price or 0)
    return round(material + power, 2)


def _colour(c):
    return re.sub(r"[^0-9a-f]", "", str(c or "").lower())[:6]


class Prices:
    """Price per kg by cloud spool id; the cloud's filament library has no price field."""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "spool_prices.json")
        self.lock = threading.RLock()
        self.items = read_json(self.path, {})

    def get(self, spool_id):
        return self.items.get(spool_id)

    def set(self, spool_id, price):
        """price None removes the entry."""
        with self.lock:
            if price is None:
                self.items.pop(spool_id, None)
            else:
                self.items[spool_id] = price
            write_json(self.path, self.items)


class LoadRequests:
    """Open requests to put a library spool into a printer's AMS; one per job (older ones have no job and block nothing)."""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "load_requests.json")
        self.lock = threading.RLock()
        self.items = read_json(self.path, [])

    def add(self, req):
        """(request, created); the job's open request is returned as it is."""
        with self.lock:
            old = next((r for r in self.items if r.get("job_id") == req["job_id"]), None)
            if old:
                return old, False
            self.items.append(req)
            write_json(self.path, self.items)
            return req, True

    def remove(self, req_id):
        with self.lock:
            self.items = [r for r in self.items if r["id"] != req_id]
            write_json(self.path, self.items)

    def drop_jobs(self, active):
        """Close the requests of jobs that are no longer waiting; active is the set of waiting job ids."""
        with self.lock:
            left = [r for r in self.items if not r.get("job_id") or r["job_id"] in active]
            if len(left) != len(self.items):
                self.items = left
                write_json(self.path, self.items)

    def settle(self, spools):
        """Close the requests whose spool the cloud library shows in the requested printer."""
        with self.lock:
            left = [r for r in self.items if not any(
                s["id"] == r["spool_id"] and s.get("printer") == r["printer"] and s.get("in_printer") for s in spools)]
            if len(left) != len(self.items):
                self.items = left
                write_json(self.path, self.items)


def in_tray(spools, serial, unit, slot):
    return next((s for s in spools if s["printer"] == serial and s["unit"] == unit and s["slot"] == slot), None)


def match(spools, serial, ftype, colour, tray_hint=None):
    """The spool in the tray the slice was made for, else a loaded spool of that type and colour."""
    if tray_hint:
        hit = in_tray(spools, serial, *tray_hint)
        if hit and hit["type"].lower() == ftype.lower():
            return hit
    return next((s for s in spools if s["printer"] == serial and s["type"].lower() == ftype.lower()
                 and (not colour or _colour(s["colour"]) == _colour(colour))), None)
