"""Send a print file to a printer through the bridge, and remember the result per printer."""
import base64
import os
import threading
from datetime import datetime, timezone

import files
from actions import ActionError
from gcode_guard import check_gcode_3mf
from queue_registry import read_json, write_json

BUSY = {"RUNNING", "PAUSE", "PREPARE"}
OK_RESULTS = {"sent", "success"}
SEND_TIMEOUT = 300  # seconds; a large file plus the bridge's own work. The bridge gives up on the daemon after 280 s.
ADVANCED = ("flow_cali", "vibration_cali", "layer_inspect", "nozzle_offset_cali", "bed_type", "ext_change_assist")
TRI = {"on": True, "off": False}  # the action's default for bed_leveling is "on"; the bridge takes true


class Unsure(ActionError):
    """The send ended in a timeout: the file may have reached the printer, so the queue must not send it again."""
    unsure = True


def error_text(status, body):
    if isinstance(body, dict):
        for key in ("error", "message", "detail"):
            if body.get(key):
                return str(body[key])
    return "Add-on answered HTTP %s: %s" % (status, body)


class Sender:
    def __init__(self, app):
        self.app = app
        self.path = os.path.join(app.data_dir, "sender.json")
        self.lock = threading.Lock()
        self.results = read_json(self.path, {})
        self.sending = {}  # serial -> file name while the bridge call runs, for the progress steps
        self.printer_locks = {}  # serial -> held from the busy check to the bridge's answer: one send per printer

    def result(self, serial):
        """What the cloud sender sensor shows for a printer."""
        return dict({"result": "idle", "last_file": None, "last_path": None, "last_time": None, "last_error": None},
                    **self.results.get(serial, {}))

    def _record(self, serial, filename, error, path=None):
        with self.lock:
            r = self.results.setdefault(serial, {})
            r.update(result="error" if error else "sent", last_error=error, last_file=filename,
                     last_time=datetime.fromtimestamp(self.app.clock(), timezone.utc).isoformat())
            if path:
                r["last_path"] = path
            write_json(self.path, self.results)

    def print_file(self, data):
        """data: the print_file action's fields (device_id or serial, file, options, force). Returns the bridge's answer."""
        serial = data.get("serial") or data.get("device_id")
        if not serial:
            raise ValueError("must contain at least one of device_id, serial.")
        with self.lock:
            lock = self.printer_locks.setdefault(serial, threading.Lock())
        if not lock.acquire(blocking=False):  # an action and a queue start must not both reach one printer
            raise ActionError(409, "Printer %s is already sending a file" % serial)
        try:
            return self._print_file(serial, data)
        finally:
            lock.release()

    def _print_file(self, serial, data):
        # The slicer writes sliced prints into the app share, which is not always /share.
        path = files.resolve(data["file"], self.app.media_dir, self.app.media_roots + [self.app.share])
        filename = os.path.basename(path)
        printer = next((p for p in self.app.bridge_printers() if p.get("serial") == serial), {})
        status = str(printer.get("print_status") or "").upper()
        if status in BUSY and not data.get("force"):
            raise ActionError(409, "Printer %s is %s; refusing to send a new print" % (serial, status))
        # force overrides only the busy check: a file that cannot load filament prints air.
        model = printer.get("dev_product_name") or printer.get("dev_model_name")
        problems = check_gcode_3mf(path, model, data.get("use_ams", True))
        if problems:
            text = "Refusing to send %s: %s" % (filename, " ".join(problems))
            self._record(serial, filename, text)
            raise ActionError(409, text)
        body = {"serial": serial, "filename": filename,
                "file_b64": base64.b64encode(files.read(path)).decode("ascii"),
                "timelapse": data.get("timelapse", False), "use_ams": data.get("use_ams", True),
                "bed_leveling": TRI.get(data.get("bed_leveling", True), data.get("bed_leveling", True)),
                **{k: TRI.get(data[k], data[k]) for k in ADVANCED if k in data}}
        self.sending[serial] = filename
        try:
            status_code, answer = self.app.bridge_json("POST", "/api/print", body, timeout=SEND_TIMEOUT)
        except ActionError as e:
            text = "Add-on not reachable: %s" % str(e).removeprefix("bridge is not reachable: ")
            self._record(serial, filename, text)
            raise Unsure(502, text)
        finally:
            self.sending.pop(serial, None)
        if status_code == 200 and isinstance(answer, dict) and answer.get("result") in OK_RESULTS:
            self._record(serial, filename, None, path)
            if body["use_ams"]:
                self.app.watchdog.started(serial, filename)
            return answer
        text = error_text(status_code, answer)
        self._record(serial, filename, text)
        raise (Unsure if status_code == 504 else ActionError)(502, text)
