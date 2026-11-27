"""Second safety layer after the G-code guard: pause a Druckwerk print that runs without filament.

It reads the vendor integration's sensors through Home Assistant, not the bridge status, which can be hours stale.
Only prints started through started() are watched.
"""
import sys
import threading

CHECK_EVERY = 10  # seconds; the app's ticker calls tick() at this pace
AIR_FOR = 120  # no filament this long while "printing": pause
GIVE_UP_WAITING = 15 * 60  # a print that never reached running was not started
NO_FILAMENT_STAGES = {"printing", "idle"}
NOT_A_TRAY = {"none", "unknown", "unavailable"}
KEYS = ("print_status", "stage", "active_tray")  # unique_id suffixes after <serial>_ in the vendor integration


class Watchdog:
    def __init__(self, app):
        self.app = app
        self.watches = {}
        self.lock = threading.Lock()

    def started(self, serial, filename):
        """Call when a print with use_ams was sent."""
        ids = {k: self.app.integration.vendor_entity(serial, "sensor", k) for k in KEYS}
        if not all(ids.values()):
            print("druckwerk: air-print watchdog off for %s: the vendor integration sensors not found" % serial,
                  file=sys.stderr, flush=True)
            return
        with self.lock:
            self.watches[serial] = {"serial": serial, "filename": filename, "at": self.app.clock(), "ids": ids,
                                    "seen_running": False, "changing_filament": False, "air_since": None}

    def on_state(self, entity_id, state):
        # changing_filament lasts about a second, shorter than the check interval.
        if state != "changing_filament":
            return
        with self.lock:
            for w in self.watches.values():
                if entity_id == w["ids"]["stage"]:
                    w["changing_filament"] = True

    def _value(self, w, key, states):
        s = states.get(w["ids"][key])
        return s["state"] if s else None

    def tick(self):
        with self.lock:
            watches = list(self.watches.values())
        if not watches:
            return
        states = self.app.ha.states(max_age=0)
        for w in watches:
            self._check(w, states)

    def _stop(self, w):
        with self.lock:
            self.watches.pop(w["serial"], None)

    def _check(self, w, states):
        now = self.app.clock()
        status = self._value(w, "print_status", states)
        if status == "running":
            w["seen_running"] = True
        elif w["seen_running"] and status != "pause":
            return self._stop(w)  # finished, failed or cancelled
        elif not w["seen_running"] and now - w["at"] > GIVE_UP_WAITING:
            return self._stop(w)  # sent, but never started
        if status != "running":
            w["air_since"] = None
            return
        tray = self._value(w, "active_tray", states)
        if w["changing_filament"] and tray not in NOT_A_TRAY and tray is not None:
            return self._stop(w)  # filament is loaded
        # idle counts like printing: the bad print never left stage idle (it printed air for an hour with stage idle and
        # tray none), while a good print passes through other stages. Only the literal "none" counts: unknown or
        # unavailable means the vendor integration lost the printer.
        if tray == "none" and self._value(w, "stage", states) in NO_FILAMENT_STAGES:
            if w["air_since"] is None:
                w["air_since"] = now
            if now - w["air_since"] >= AIR_FOR:
                self._stop(w)  # pause once, never again for this print
                self._pause(w)
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
        self.app.ha.call_service("persistent_notification", "create", {
            "message": "%s is printing %s without filament: for 120 s no filament was loaded (active tray none, "
                       "stage printing or idle). %s" % (printer, w["filename"], outcome),
            "title": "Print without filament", "notification_id": "druckwerk_air_" + serial})
