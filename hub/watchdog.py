"""Second safety layer after the G-code guard: pause a Druckwerk print that runs without filament.

It reads the printer's state from the bridge (hub/printer_state.py). Only prints started through started() are watched.
"""
import sys
import threading

CHECK_EVERY = 10  # seconds; the app's ticker calls tick() at this pace
AIR_FOR = 120  # no filament this long while "printing": pause
GIVE_UP_WAITING = 15 * 60  # a print that never reached running was not started
NO_FILAMENT_STAGES = {"printing", "idle"}
NOT_A_TRAY = {"none", "unknown", "unavailable"}


class Watchdog:
    def __init__(self, app):
        self.app = app
        self.watches = {}
        self.lock = threading.Lock()

    def started(self, serial, filename):
        """Call when a print with use_ams was sent."""
        with self.lock:
            self.watches[serial] = {"serial": serial, "filename": filename, "at": self.app.clock(),
                                    "seen_running": False, "changing_filament": False, "air_since": None}

    def on_state(self, entity_id, state):
        # changing_filament lasts about a second, shorter than the check interval.
        if state != "changing_filament":
            return
        with self.lock:
            for w in self.watches.values():
                p = self.app.registry.get(w["serial"])
                if p and entity_id == self.app.entity(p, "stage"):
                    w["changing_filament"] = True

    def tick(self):
        with self.lock:
            watches = list(self.watches.values())
        if not watches:
            return
        for w in watches:
            self._check(w, self.app.printer_status(w["serial"]))

    def _stop(self, w):
        with self.lock:
            self.watches.pop(w["serial"], None)

    def _check(self, w, view):
        now = self.app.clock()
        if view is None:
            return  # the bridge could not say; the next tick asks again
        status = view["status"]
        if status == "running":
            w["seen_running"] = True
        elif w["seen_running"] and status != "pause":
            return self._stop(w)  # finished, failed or cancelled
        elif not w["seen_running"] and now - w["at"] > GIVE_UP_WAITING:
            return self._stop(w)  # sent, but never started
        if status != "running":
            w["air_since"] = None
            return
        tray = view["active_tray"]["name"] if view["active_tray"] else "none"
        if view["stage"] == "changing_filament":
            w["changing_filament"] = True
        if w["changing_filament"] and tray not in NOT_A_TRAY:
            return self._stop(w)  # filament is loaded
        # idle counts like printing: the bad print never left stage idle (it printed air for an hour with stage idle and
        # tray none), while a good print passes through other stages.
        if tray == "none" and view["stage"] in NO_FILAMENT_STAGES:
            if w["air_since"] is None:
                w["air_since"] = now
            if now - w["air_since"] >= AIR_FOR:
                self._stop(w)  # pause once, never again for this print
                # The bridge call takes up to 60 s; the ticker must go on with the other printers.
                threading.Thread(target=self._pause, args=(w,), name="druckwerk-pause", daemon=True).start()
        else:
            w["air_since"] = None

    def _pause(self, w):
        serial = w["serial"]
        name = (self.app.registry.get(serial) or {}).get("name")
        printer = "%s (%s)" % (name, serial) if name else serial
        try:
            self.app.bridge_ok("POST", "/api/pause/" + serial, {}, timeout=60)
            outcome = "The print was paused."
        except Exception as e:  # noqa: BLE001 - the person must hear that the pause failed
            outcome = "Pausing FAILED (%s); stop the print yourself." % e
        try:
            self.app.ha.call_service("persistent_notification", "create", {
                "message": "%s is printing %s without filament: for 120 s no filament was loaded (active tray none, "
                           "stage printing or idle). %s" % (printer, w["filename"], outcome),
                "title": "Print without filament", "notification_id": "druckwerk_air_" + serial})
        except Exception as e:  # noqa: BLE001 - this thread has no caller that would log it
            print("druckwerk: air-print notification for %s failed: %s. %s" % (printer, e, outcome), file=sys.stderr, flush=True)
