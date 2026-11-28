"""Nozzles: what is installed (read live from the vendor integration's sensors) and requests to change it."""
import json
import os
import threading

from queue_registry import write_json


def installed(states, prefix):
    """{"diameter": "0.4", "type": "hardened_steel"} from the printer's nozzle sensors, or None when the size is unknown."""
    def read(key):
        s = (states.get("sensor.%s_nozzle_%s" % (prefix, key)) or {}).get("state")
        return None if s in (None, "", "unknown", "unavailable") else str(s)
    size = read("size")
    try:
        size = "%g" % float(size)  # "0.40" and "0.4" are one size
    except (TypeError, ValueError):
        return None
    return {"diameter": size, "type": read("type") or ""}


def library(printer, now):
    """The owner's nozzles, with the installed one added when it is not among them."""
    out = list(printer.get("nozzles") or [])
    if now and not any(n["diameter"] == now["diameter"] and n["type"] == now["type"] for n in out):
        out.append(now)
    return out


def matches(now, diameter, type_=""):
    return bool(now) and now["diameter"] == diameter and (not type_ or now["type"] == type_)


class NozzleRequests:
    """Open requests to change a printer's nozzle; one per (printer, diameter, type)."""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "nozzle_requests.json")
        self.lock = threading.RLock()
        self.items = []
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.items = json.load(f)

    def add(self, req):
        """(request, created); an open request for the same nozzle and printer is returned as it is."""
        with self.lock:
            old = next((r for r in self.items if (r["printer"], r["diameter"], r["type"]) == (req["printer"], req["diameter"], req["type"])), None)
            if old:
                return old, False
            self.items.append(req)
            write_json(self.path, self.items)
            return req, True

    def remove(self, req_id):
        with self.lock:
            self.items = [r for r in self.items if r["id"] != req_id]
            write_json(self.path, self.items)

    def settle(self, now_of):
        """Close the requests whose nozzle the printer now has; now_of(serial) is its installed nozzle or None."""
        with self.lock:
            left = [r for r in self.items if not matches(now_of(r["printer"]), r["diameter"], r["type"])]
            if len(left) != len(self.items):
                self.items = left
                write_json(self.path, self.items)
