#!/usr/bin/env python3
"""Hub app on the ingress port: the slicer server with the hub API (app.py) mounted on it.

HA ingress reaches one port only. It serves the web UI (hub/ui) at / and the API (app.py). The bridge keeps its own
unchanged routes on 8098 for the integration and stays reachable for HA admins under /api/bridge/.
"""
import json
import os
import sys
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I drops the script dir
import slicer_server as s
import watchdog
from app import App, log
from ha import HA
from plates_inbox import PlatesInbox

env = os.environ.get
BRIDGE = ("127.0.0.1", int(env("DW_PORT", "8098")))
SUPERVISOR = env("HUB_SUPERVISOR_URL", "http://supervisor")


def supervisor(path, body=None):
    req = urllib.request.Request(SUPERVISOR + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + env("SUPERVISOR_TOKEN", ""),
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"null")


def ingress_ips():
    """An empty variable must not trust each client: no address in the set would turn the check off."""
    ips = [i.strip() for i in env("HUB_INGRESS_IPS", "").split(",") if i.strip()]
    return ips or ["172.30.32.2"]


def hostname():
    return env("HUB_HOSTNAME") or supervisor("/addons/self/info")["data"]["hostname"]


def main():
    ha = HA(env("SLICER_CORE_URL", "http://supervisor/core/api"), env("HUB_HA_WS", "ws://supervisor/core/websocket"), env("SUPERVISOR_TOKEN", ""))
    app = App(ha, env("HUB_DATA", "/data"), s.SHARE, BRIDGE, s, hostname, int(env("SLICER_PORT", "8099")),
              ui_dir=env("HUB_UI", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")),
              trusted_ips=ingress_ips())
    app.install()

    stop = threading.Event()
    threading.Thread(target=app.run_ticker, args=(stop, watchdog.CHECK_EVERY), daemon=True).start()
    threading.Thread(target=ha.watch_state_changes, args=(app.on_state, stop), daemon=True).start()
    threading.Thread(target=app.notifier.run, args=(stop,), daemon=True).start()
    app.plates_inbox = PlatesInbox.from_options(app, env("HUB_DATA", "/data"))
    threading.Thread(target=app.plates_inbox.run, args=(stop,), daemon=True).start()
    threading.Thread(target=ha.watch_events, args=("mobile_app_notification_action", app.notifier.on_action, stop, True),
                     daemon=True).start()

    def announce():
        while not stop.is_set():
            try:
                supervisor("/discovery", app.discovery())
                return log("discovery sent")
            except Exception as e:  # noqa: BLE001 - Supervisor may not be ready yet
                log("discovery failed, retrying: %s" % e)
                stop.wait(30)
    threading.Thread(target=announce, daemon=True).start()
    s.main()


if __name__ == "__main__":
    main()
