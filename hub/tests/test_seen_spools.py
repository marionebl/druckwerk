"""The hub records the filament it sees in the AMS trays as spools (source "seen"), without touching spools people made."""
from hubfakes import ADMIN, SERIAL, SERIAL2, st

UID = "A1B2C3D4E5F60718"


def trays(hub):
    return {s["slot"]: s for s in hub.app.spools.items if s["printer"] == SERIAL}


def poll(hub):
    hub.now += 120  # past the throttle
    hub.app.try_track_filament()


def test_new_trays_create_one_spool_each_and_a_second_poll_adds_none(hub):
    hub.setup_printers()
    poll(hub)
    items = hub.app.spools.items
    assert len(items) == 2
    t = trays(hub)
    assert (t[0]["type"], t[0]["colour"], t[0]["source"], t[0]["unit"], t[0]["grams_left"], t[0]["name"]) == ("PETG", "ADB1B2", "seen", 0, 800, "PETG HF")
    assert (t[1]["type"], t[1]["colour"], t[1]["grams_left"]) == ("PLA", "BB3D43", 550)
    ids = [s["id"] for s in items]
    poll(hub)
    assert [s["id"] for s in hub.app.spools.items] == ids


def test_poll_is_throttled(hub):
    hub.setup_printers()
    hub.app.try_track_filament()
    hub.app.spools.items.clear()
    hub.now += 10
    hub.app.try_track_filament()
    assert hub.app.spools.items == []  # inside the throttle window nothing ran


def test_a_user_spool_that_matches_is_reused_not_duplicated(hub):
    hub.setup_printers()
    mine = hub.app.spools.add(brand="Acme", type="PETG", colour="ADB1B2", grams_left=640, grams_total=1000, price_per_kg=19)
    poll(hub)
    assert len([s for s in hub.app.spools.items if s["type"] == "PETG"]) == 1
    assert (mine["printer"], mine["unit"], mine["slot"]) == (SERIAL, 0, 0)
    assert (mine["grams_left"], mine["brand"], mine["price_per_kg"]) == (640, "Acme", 19)  # the person's data stays


def test_a_spool_the_user_linked_is_left_alone(hub):
    hub.setup_printers()
    mine = hub.app.spools.add(brand="Acme", type="PLA", colour="111111", grams_left=100, printer=SERIAL, unit=0, slot=0)
    poll(hub)
    assert mine["grams_left"] == 100 and mine["type"] == "PLA"
    assert not [s for s in hub.app.spools.items if s["printer"] == SERIAL and s["slot"] == 0 and s is not mine]
    assert len(hub.app.spools.items) == 2  # the user's spool and the seen PLA of slot 1


def test_a_moved_tag_updates_where_the_spool_is_loaded(hub):
    hub.setup_printers()
    hub.ha.s["sensor.ams_tray_1"]["attributes"]["tag_uid"] = UID
    poll(hub)
    spool = trays(hub)[0]
    assert spool["uid"] == UID
    first_loaded = spool["loaded_at"]
    # The spool moves from slot 1 to slot 2: tray 1 is empty, tray 2 has the tag.
    hub.ha.s["sensor.ams_tray_1"] = st("Empty", slot=1, type="", color="00000000", remain=-1, empty=True)
    hub.ha.s["sensor.ams_tray_2"] = st("PETG HF", slot=2, type="PETG", color="#ADB1B2FF", remain=70, empty=False, tag_uid=UID)
    poll(hub)
    assert len([s for s in hub.app.spools.items if s["uid"] == UID]) == 1 if all("uid" in s for s in hub.app.spools.items) else True
    same = [s for s in hub.app.spools.items if s.get("uid") == UID]
    assert len(same) == 1 and same[0]["id"] == spool["id"]
    assert (same[0]["printer"], same[0]["unit"], same[0]["slot"]) == (SERIAL, 0, 1)
    assert same[0]["loaded_at"] > first_loaded and same[0]["grams_left"] == 700


def test_an_emptied_tray_unloads_a_seen_spool_but_keeps_it(hub):
    hub.setup_printers()
    poll(hub)
    n = len(hub.app.spools.items)
    hub.ha.s["sensor.ams_tray_2"] = st("Empty", slot=2, type="", color="00000000", remain=-1, empty=True)
    poll(hub)
    assert len(hub.app.spools.items) == n
    unloaded = next(s for s in hub.app.spools.items if s["type"] == "PLA")
    assert unloaded["printer"] is None and unloaded["unit"] is None and unloaded["slot"] is None


def test_unreadable_trays_change_nothing(hub):
    hub.setup_printers()
    poll(hub)
    before = [dict(s) for s in hub.app.spools.items]
    hub.ha.s = {k: v for k, v in hub.ha.s.items() if "ams_tray" not in k}
    poll(hub)
    assert hub.app.spools.items == before


def test_the_filament_list_returns_seen_spools(hub):
    hub.setup_printers()
    poll(hub)
    got = hub.req("GET", "/api/spools", user=ADMIN)[1]
    assert sorted(s["type"] for s in got) == ["PETG", "PLA"] and all(s["source"] == "seen" for s in got)
    hub.grant("u-user", SERIAL)
    assert len(hub.req("GET", "/api/spools", user="u-user")[1]) == 2  # a user with a grant sees what is loaded
    assert hub.req("GET", "/api/spools", user="u-other")[1] == []


def test_seen_spools_can_be_edited_like_any_spool(hub):
    hub.setup_printers()
    poll(hub)
    s = trays(hub)[0]
    st_, body = hub.req("PUT", "/api/spools/" + s["id"], {"price_per_kg": 25}, user=ADMIN)
    assert st_ == 200 and body["price_per_kg"] == 25 and body["source"] == "seen"
