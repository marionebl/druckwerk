"""Air-print watchdog: replays of the two real prints from 2026-11-22 (state sequences from HA history)."""
import pytest

import watchdog
from hubfakes import SERIAL, FakeBridge, make_3mf

FILE = "benchy.gcode.3mf"
STAGES = {"idle": -1, "printing": 0, "changing_filament": 4, "cooling_chamber": 29, "homing_toolhead": 13,
          "calibrating_extrusion": 8, "auto_bed_leveling": 1}
BAD = [(0, "prepare", "idle", "none"), (1, "running")]  # what the bad print looked like: stage idle, tray none, for an hour


@pytest.fixture
def hub(hub):
    hub.setup_printers()
    set_states(hub, "idle", "idle", "none")
    return hub


def set_states(hub, status=None, stage=None, tray=None):
    """tray "unavailable" is a printer the bridge cannot read."""
    fields = {}
    if status:
        fields["gcode_state"] = status.upper()
    if stage:
        fields["stg_cur"] = STAGES[stage]
    if tray is not None:
        FakeBridge.status_fail = {SERIAL} if tray == "unavailable" else set()
        fields["ams"] = {"tray_now": "255" if tray == "none" else "1"}
    hub.set_print(**fields)
    if stage:
        hub.app.on_state(hub.app.entity(hub.app.registry.get(SERIAL), "stage"), stage)  # the websocket watcher


def replay(hub, seconds, timeline=()):
    """Advance the clock in 1 s steps and tick every CHECK_EVERY; timeline is [(second, status, stage, tray)]."""
    pending = sorted(timeline, key=lambda e: e[0])
    for sec in range(1, seconds + 1):
        while pending and pending[0][0] <= sec:
            set_states(hub, *pending.pop(0)[1:])
        hub.now += 1
        if sec % watchdog.CHECK_EVERY == 0:
            hub.app.watchdog.tick()


def pauses():
    return [p for p, _ in FakeBridge.log if p == "/api/pause/" + SERIAL]


def notes(hub):
    return [c[2] for c in hub.ha.calls if c[:2] == ("persistent_notification", "create")]


def test_bad_print_is_paused_once_after_120s_with_notification(hub):
    hub.app.watchdog.started(SERIAL, FILE)
    replay(hub, 100, BAD)
    assert pauses() == [] and notes(hub) == []  # still inside the 120 s grace
    replay(hub, 40)
    assert len(pauses()) == 1
    (note,) = notes(hub)
    assert note["title"] == "Print without filament" and note["notification_id"] == "druckwerk_air_" + SERIAL
    assert "Lab (%s)" % SERIAL in note["message"] and FILE in note["message"] and "The print was paused." in note["message"]
    set_states(hub, "pause")  # the printer now reports pause; nothing more may be sent
    replay(hub, 600)
    assert len(pauses()) == 1 and len(notes(hub)) == 1


def test_a_failed_pause_is_in_the_notification(hub):
    FakeBridge.post_answer = {"/api/pause/" + SERIAL: (503, {"error": "daemon not reachable"})}
    hub.app.watchdog.started(SERIAL, FILE)
    replay(hub, 150, BAD)
    assert "Pausing FAILED (daemon not reachable); stop the print yourself." in notes(hub)[0]["message"]


def test_good_print_is_not_paused(hub):
    hub.app.watchdog.started(SERIAL, FILE)
    good = [(0, "prepare", "idle", "none"), (1, "running"), (20, None, "cooling_chamber"), (40, None, "homing_toolhead"),
            (110, None, "changing_filament"), (111, None, None, "Acme PLA Matte"), (140, None, "calibrating_extrusion"),
            (200, None, "auto_bed_leveling"), (290, None, "printing")]
    replay(hub, 900, good)
    assert pauses() == [] and notes(hub) == []


def test_slow_filament_load_still_idle_before_changing_filament_is_not_paused(hub):
    hub.app.watchdog.started(SERIAL, FILE)
    replay(hub, 700, BAD + [(100, None, "changing_filament"), (102, None, None, "Acme PLA Matte"), (103, None, "printing")])
    assert pauses() == []


def test_print_not_started_by_the_app_is_not_paused(hub):
    replay(hub, 600, BAD)  # no started(): somebody printed from their own app
    assert pauses() == [] and notes(hub) == []


def test_watch_ends_when_print_leaves_running(hub):
    hub.app.watchdog.started(SERIAL, FILE)
    replay(hub, 60, BAD + [(60, "finish")])
    replay(hub, 30, [(0, "running")])  # the next print is someone else's
    replay(hub, 300)
    assert pauses() == []


def test_unavailable_tray_is_not_enough(hub):
    hub.app.watchdog.started(SERIAL, FILE)
    replay(hub, 400, [(0, "running", "printing", "unavailable")])
    assert pauses() == []


def test_gives_up_after_15_minutes_without_a_running_print(hub):
    hub.app.watchdog.started(SERIAL, FILE)
    replay(hub, watchdog.GIVE_UP_WAITING - 10)
    assert SERIAL in hub.app.watchdog.watches
    replay(hub, 30)
    assert hub.app.watchdog.watches == {}
    replay(hub, 300, BAD)  # a later print by someone else
    assert pauses() == []


def test_the_watch_needs_no_vendor_entities(hub):
    hub.ha.entities, hub.ha.devices, hub.ha.s = [], [], {}
    hub.app.watchdog.started(SERIAL, FILE)
    replay(hub, 140, BAD)
    assert len(pauses()) == 1


def test_a_print_with_ams_starts_a_watch_and_one_without_does_not(hub):
    make_3mf(hub.media / "druckwerk" / FILE)
    run = lambda **d: hub.req("POST", "/api/actions/print_file", dict({"serial": SERIAL, "file": FILE}, **d), secret=True)
    assert run(use_ams=False)[0] == 200 and hub.app.watchdog.watches == {}
    assert run()[0] == 200 and hub.app.watchdog.watches[SERIAL]["filename"] == FILE
