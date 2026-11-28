"""What the integration's entities show, per printer (contract: hub/API.md, entity table: entities.py)."""
import hashlib
import os

import actions
import files
import preview
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

    def wanted(self, serial, last_path, task):
        """The file for the job the printer reports (its task name); the last sent file if it reports none."""
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

    @staticmethod
    def light(status):
        mode = ((status or {}).get("lights") or {}).get("chamber_light")
        return mode == "on" if mode in ("on", "off") else None

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
        out = {}
        for p in self.app.listed_printers():
            serial = p.get("serial")
            if not serial:
                continue
            r = self.app.sender.result(serial)
            status = self.app.printer_status(serial)
            path = self.wanted(serial, r["last_path"], (status or {}).get("task_name"))
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
                    "chamber_light": {"on": self.light(status)},
                    "queue": {"items": self.queue_items(serial)},
                },
            }
        return {"manifest_revision": actions.manifest_revision(), "printers": out}
