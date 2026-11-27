"""What the integration's entities show, per printer (contract: hub/API.md, entity table: entities.py)."""
import hashlib
import os

import actions
import files
import preview
from externals import HA_VENDOR_DOMAIN as VENDOR
from queue_engine import WAITING

JPEG_PRODUCTS = ("P1", "A1")  # P1P, P1S, A1, A1 mini send JPEG frames, which WebRTC cannot carry
UNKNOWN = (None, "", "unknown", "unavailable")
PRINTER = "/api/integration/printers/"


def what(job):
    """The file of a print job, or the settings of a drying job."""
    d = job.get("dry")
    if job.get("kind") == "dry" and isinstance(d, dict):
        unit = "AMS %d" % (d.get("ams_id", 0) + 1) if d.get("ams_id", 0) < 128 else "AMS HT"
        return "Dry %s, %s, %s C, %s h" % (unit, d.get("filament") or "any filament", d.get("temp"), d.get("hours"))
    return "File: %s" % job.get("file")


class Integration:
    def __init__(self, app):
        self.app = app
        self.previews = {}  # serial -> (key of the file, PNG or None)

    def file_list(self):
        return self.app._cached("files", 5, lambda: files.scan(self.app.media_dir))

    def vendor_entity(self, serial, domain, key, suffix=False):
        """Entity id of the vendor integration for this printer: unique_id <serial>_<key>, or (suffix) any of its device ending in key."""
        devices, entities = self.app.registries()
        device = next((d["id"] for d in devices if [VENDOR, serial] in [list(i) for i in d.get("identifiers", [])]), None)
        for e in entities:
            if e.get("platform") != VENDOR or not e["entity_id"].startswith(domain + "."):
                continue
            uid = e.get("unique_id") or ""
            if uid == "%s_%s" % (serial, key) or (suffix and device and e.get("device_id") == device and uid.endswith(key)):
                return e["entity_id"]
        return None

    def wanted(self, serial, last_path, states):
        """The file for the job the printer reports; the last sent file if it reports none."""
        entity = self.vendor_entity(serial, "sensor", "subtask_name")
        task = (states.get(entity) or {}).get("state") if entity else None
        if task in UNKNOWN:
            return last_path
        if last_path and os.path.basename(last_path).removesuffix(files.SUFFIX) == task:
            return last_path
        # Prefer the top-level copy when the name exists in several folders.
        names = sorted(self.file_list(), key=lambda f: f.count("/"))
        match = next((f for f in names if os.path.basename(f).removesuffix(files.SUFFIX) == task), None)
        return os.path.join(self.app.media_dir, match) if match else None

    def preview(self, serial, path):
        """(token, PNG) of a file's plate picture; both None without one. Drawn once per file version."""
        try:
            key = "%s:%s" % (path, os.stat(path).st_mtime_ns) if path else None
        except OSError:
            key = None
        have = self.previews.get(serial)
        if not have or have[0] != key:
            have = self.previews[serial] = (key, preview.plate_png(path) if key else None)
        token = hashlib.sha256(key.encode()).hexdigest()[:12] if key and have[1] else None
        return token, have[1]

    def light(self, serial, states):
        entity = self.vendor_entity(serial, "light", "chamber_light", suffix=True)
        state = (states.get(entity) or {}).get("state") if entity else None
        return state == "on" if state in ("on", "off") else None

    def queue_items(self, serial):
        with self.app.queue.lock:
            jobs = [j for j in self.app.queue.jobs
                    if j.get("state") in WAITING and serial in (j.get("target"), j.get("printer"))]
        return [{"uid": j["id"], "summary": str(j.get("name") or j.get("file")),
                 "description": "%s\nOwner: %s%s" % (what(j), j.get("owner_name") or "unknown",
                                                     " (needs approval)" if j.get("state") == "pending_approval" else "")}
                for j in jobs]

    def camera(self, serial, printer):
        live = printer.get("live")
        online = isinstance(live, dict) and bool(live.get("cached"))
        return {"online": online, "stream": "/api/camera/%s/live" % serial if online else None,
                "transcode": str(printer.get("dev_product_name") or "").startswith(JPEG_PRODUCTS),
                "snapshot": PRINTER + serial + "/snapshot"}

    def state(self):
        states = self.app.safe_states()
        out = {}
        for p in self.app.bridge_printers():
            serial = p.get("serial")
            if not serial:
                continue
            r = self.app.sender.result(serial)
            path = self.wanted(serial, r["last_path"], states)
            token, _png = self.preview(serial, path)
            out[serial] = {
                "name": p.get("name") or serial,
                "model": p.get("dev_product_name") or p.get("dev_model_name"),
                "entities": {
                    "print_file": {"options": self.file_list()},
                    "cloud_sender": {"state": r["result"],
                                     "attributes": {"account_email": p.get("email"), "last_file": r["last_file"],
                                                    "last_time": r["last_time"], "last_error": r["last_error"]}},
                    "cloud_camera": self.camera(serial, p),
                    "current_print": {"token": token, "file_path": path, "has_preview": token is not None},
                    "chamber_light": {"on": self.light(serial, states)},
                    "queue": {"items": self.queue_items(serial)},
                },
            }
        return {"manifest_revision": actions.manifest_revision(), "printers": out}
