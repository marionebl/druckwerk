"""Events to Home Assistant: bus events, and mobile notifications to the admins who are at home.

App.emit() queues an event; run() sends it from its own thread so a slow Home Assistant never holds up a request.
"""
import json
import os
import queue
import sys

from queue_registry import write_json

EVENT_PREFIX = "druckwerk_"  # bus event type: druckwerk_<event type>
ACTION_PREFIX = "DRUCKWERK_START_"  # notification button action: DRUCKWERK_START_<serial>
TITLE = "Druckwerk"


def log(msg):
    print("druckwerk: " + msg, file=sys.stderr, flush=True)


class Notifier:
    def __init__(self, app):
        self.app = app
        self.path = os.path.join(app.data_dir, "integration.json")
        self.todo = queue.SimpleQueue()
        self.options = {"recipients": {}}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.options.update(json.load(f))

    # -- options the integration pushes
    def set_options(self, body):
        r = body.get("recipients")
        if not isinstance(r, dict) or not all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in r.items()):
            raise ValueError("recipients must be an object of person entity id to notify service name")
        self.options = {"recipients": dict(r)}
        write_json(self.path, self.options)
        return self.options

    # -- events
    def put(self, event):
        self.todo.put(event)

    def flush(self):
        """Handle all queued events now (tests)."""
        while True:
            try:
                event = self.todo.get_nowait()
            except queue.Empty:
                return
            self.handle(event)

    def run(self, stop):
        while not stop.is_set():
            try:
                event = self.todo.get(timeout=1)
            except queue.Empty:
                continue
            self.handle(event)

    def handle(self, event):
        etype, serial = event.get("type"), event.get("serial")
        if not etype:
            return
        try:
            self.app.ha.fire_event(EVENT_PREFIX + etype, event)
        except Exception as e:  # noqa: BLE001 - Home Assistant may be down; the notifications below still try
            log("cannot fire %s%s: %s" % (EVENT_PREFIX, etype, e))
        job = event.get("job") or {}
        try:
            if etype == "job_done":
                nxt = self.next_job(serial, exclude=job.get("id"))
                if nxt:
                    self.notify_admins(serial, "Plate clear? Start next: %s" % (nxt.get("name") or nxt.get("file")),
                                       [{"action": ACTION_PREFIX + serial, "title": "Plate clear, start"}])
            elif etype == "load_requested":
                r = event.get("request") or {}
                note = (": " + r["note"]) if r.get("note") else ""
                self.notify_owners(serial, "%s asks to load %s into %s%s" % (
                    r.get("user_name") or "Someone", r.get("spool_label"), r.get("printer_name") or serial, note))
            elif etype == "nozzle_requested":
                r = event.get("request") or {}
                note = (": " + r["note"]) if r.get("note") else ""
                self.notify_owners(serial, "%s asks to put a %s mm %snozzle into %s%s" % (
                    r.get("user_name") or "Someone", r.get("diameter"), r.get("type", "").replace("_", " ") + " " if r.get("type") else "",
                    r.get("printer_name") or serial, note))
            elif etype == "approval_needed":
                self.notify_admins(serial, "%s needs approval to %s %s" % (
                    job.get("owner_name") or "Someone", "dry" if job.get("kind") == "dry" else "print",
                    job.get("name") or job.get("file")))
        except Exception as e:  # noqa: BLE001
            log("cannot notify for %s: %s" % (etype, e))

    def next_job(self, serial, exclude=None):
        """First queued job for this printer (or for any printer)."""
        with self.app.queue.lock:
            for job in self.app.queue.jobs:
                if job.get("state") == "queued" and job.get("target") in (serial, "any") and job.get("id") != exclude \
                        and not self.app.queue.blocked(job):
                    # start-next takes the first job; a drying there needs its own confirmation, not a plate prompt.
                    return None if job.get("kind") == "dry" else job
        return None

    def admin_ids(self, serial):
        """HA admins plus the printer's owner."""
        return self.owner_ids(serial) | {u["id"] for u in self.app.ha_users() if u["is_admin"]}

    def owner_ids(self, serial):
        """The printer's owner; HA admins only when they own it."""
        owner = (self.app.registry.get(serial) or {}).get("owner")
        return {owner} if owner else set()

    def notify_admins(self, serial, message, actions=None):
        """Notify the printer's admins who are mapped and at home (person state "home")."""
        self.push(self.admin_ids(serial), message, actions)

    def notify_owners(self, serial, message):
        ids = self.owner_ids(serial)
        if not ids:
            log("request for %s: the printer has no owner to notify" % serial)
            return
        self.push(ids, message)

    def push(self, admins, message, actions=None):
        recipients = self.options["recipients"]
        if not recipients:
            return
        states = self.app.ha.states()
        for person, service in recipients.items():
            s = states.get(person)
            if not s or s["state"] != "home" or (s.get("attributes") or {}).get("user_id") not in admins:
                continue
            try:
                self.app.ha.call_service("notify", service.removeprefix("notify."), {
                    "title": TITLE, "message": message, "data": {"actions": actions} if actions else {}})
            except Exception as e:  # noqa: BLE001 - one broken phone must not stop the others
                log("cannot notify %s: %s" % (person, e))

    def on_action(self, data):
        """A button on a notification was pressed (HA event mobile_app_notification_action)."""
        action = str(data.get("action", ""))
        if not action.startswith(ACTION_PREFIX):
            return
        try:
            self.app.queue.start(action[len(ACTION_PREFIX):], plate_clear=True)
            self.app.sync()
        except Exception as e:  # noqa: BLE001 - nobody waits for this; the queue shows the job's error
            log("start-next failed: %s" % e)
