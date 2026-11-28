"""Grams from a real slice_info.config, cost arithmetic, spools and the decrement when a job finishes."""
import os


import filament
from hubfakes import ADMIN, OTHER, SERIAL, SERIAL2, USER, FakeBridge, make_3mf


def test_grams_from_a_real_sliced_3mf(tmp_path):
    # fixtures/slice_info.config is the file the slicer 2.9.2 wrote for fixtures/test_plate.stl.
    use = filament.slice_usage(make_3mf(tmp_path / "s.gcode.3mf"))
    assert use == {"seconds": 7440, "grams": 75.22, "filaments": [{"type": "PETG", "colour": "ADB1B2", "grams": 75.22}]}


def test_grams_of_several_filaments(tmp_path):
    xml = ('<config><plate><metadata key="prediction" value="3600"/>'
           '<filament id="1" type="PLA" color="#FF0000" used_g="10.5"/><filament id="2" type="PETG" color="#00FF00" used_g="4.25"/>'
           '</plate></config>')
    use = filament.slice_usage(make_3mf(tmp_path / "m.gcode.3mf", xml))
    assert use["grams"] == 14.75 and [f["grams"] for f in use["filaments"]] == [10.5, 4.25]


def test_cost_arithmetic():
    # 75.22 g at 30/kg = 2.2566; 7440 s = 2.0667 h at 150 W (H2S) = 0.31 kWh at 0.40 = 0.124; total 2.3806
    assert filament.job_cost([75.22], [30], 7440, "H2S", 0.4) == 2.38
    assert filament.job_cost([10, 20], [20, 50], 0, "P1S", 0.4) == 1.2  # 0.2 + 1.0, no print time
    assert filament.job_cost([100], [0], 3600, "A1 mini", 1.0) == 0.08  # 80 W for one hour
    assert filament.job_cost([100], [0], 3600, "UNKNOWN", 1.0) == 0.1  # default watts
    assert filament.watts("p1s") == 105


def put_calls(hub):
    return [(path, body) for method, path, body in FakeBridge.library_calls if method == "PUT"]


def test_spools_come_from_the_cloud_library_with_the_local_price(hub):
    hub.setup_printers()
    mine = hub.cloud_spool(brand="Acme", type="PETG", name="PETG HF", colour="ADB1B2", grams_left=640, printer=SERIAL, unit=0, slot=1)
    other = hub.cloud_spool(type="PLA", printer=SERIAL2, unit=0, slot=0)
    loose = hub.cloud_spool(type="TPU", grams_left=None, grams_total=None)
    hub.app.prices.set(mine["id"], 19.5)
    got = hub.req("GET", "/api/spools", user=ADMIN)[1]
    assert [(s["id"], s["price_per_kg"]) for s in got] == [(mine["id"], 19.5), (other["id"], None), (loose["id"], None)]
    assert got[2]["grams_left"] is None  # unknown stays unknown
    assert hub.req("GET", "/api/spools", user=USER)[0] == 409  # no printer, so no cloud account to show
    hub.grant(USER, SERIAL)
    assert [s["id"] for s in hub.req("GET", "/api/spools", user=USER)[1]] == [mine["id"], loose["id"]]  # not the spool on a printer it cannot see


def test_the_library_is_cached_and_a_write_drops_the_cache(hub):
    hub.setup_printers()
    sp = hub.cloud_spool(type="PLA")
    gets = lambda: len([c for c in FakeBridge.library_calls if c[0] == "GET"])
    hub.req("GET", "/api/spools", user=ADMIN)
    hub.req("GET", "/api/spools", user=ADMIN)
    assert gets() == 1
    hub.req("PUT", "/api/spools/" + sp["id"], {"grams_left": 5}, user=ADMIN)
    hub.req("GET", "/api/spools", user=ADMIN)
    assert gets() == 2
    hub.now += 31
    hub.req("GET", "/api/spools", user=ADMIN)
    assert gets() == 3


def test_a_cloud_failure_is_an_error_not_old_data(hub):
    hub.setup_printers()
    hub.cloud_spool(type="PLA")
    assert len(hub.req("GET", "/api/spools", user=ADMIN)[1]) == 1
    hub.now += 31
    FakeBridge.library_fail = (502, "Cloud answered HTTP 401 for /v1/design-user-service/my/filament/v2")
    st, body = hub.req("GET", "/api/spools", user=ADMIN)
    assert st == 502 and "filament library" in body["error"] and "HTTP 401" in body["error"]


def test_create_edit_delete_go_to_the_cloud_and_the_price_stays_local(hub):
    hub.setup_printers()
    hub.owner(USER)  # owner = admin of that printer
    st, sp = hub.req("POST", "/api/spools", {"brand": "B", "type": "PLA", "name": "PLA Basic", "colour": "#ff0000", "grams_left": 800,
                                             "grams_total": 1000, "price_per_kg": 20}, user=USER)
    assert st == 200 and sp["id"] == "7000" and sp["price_per_kg"] == 20
    (call,) = [c for c in FakeBridge.library_calls if c[0] == "POST"]
    assert call[1] == "/api/filament?account=a_x" and "price_per_kg" not in call[2]
    assert hub.req("PUT", "/api/spools/7000", {"grams_left": 700}, user=USER)[1]["grams_left"] == 700
    assert put_calls(hub) == [("/api/filament/7000?account=a_x", {"grams_left": 700})]
    assert hub.req("PUT", "/api/spools/7000", {"price_per_kg": 25}, user=USER)[1]["price_per_kg"] == 25
    assert len(put_calls(hub)) == 1  # a price alone never reaches the cloud
    assert [s["price_per_kg"] for s in hub.req("GET", "/api/spools", user=USER)[1]] == [25]
    assert hub.req("PUT", "/api/spools/7000", {"price_per_kg": "x"}, user=USER)[0] == 400
    assert hub.req("PUT", "/api/spools/7000", {"grams_left": 5}, user=OTHER)[0] == 409  # no printer of the account
    hub.grant(OTHER, SERIAL)
    assert hub.req("PUT", "/api/spools/7000", {"grams_left": 5}, user=OTHER)[0] == 403  # sees it, may not change it
    assert hub.req("PUT", "/api/spools/999", {"grams_left": 5}, user=ADMIN)[0] == 404
    assert hub.req("DELETE", "/api/spools/7000", user=USER)[0] == 200
    assert hub.app.prices.get("7000") is None and hub.req("GET", "/api/spools", user=USER)[1] == []
    assert hub.req("DELETE", "/api/spools/7000", user=ADMIN)[0] == 404


def finish_a_print(hub):
    """Queue the real slice on SERIAL, start it, and let the printer run through to finish."""
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    jid = hub.sliced(owner=USER)
    job = hub.req("POST", "/api/jobs/%s/queue" % jid, {"target": SERIAL}, user=USER)[1]
    hub.set_print(gcode_state="IDLE")
    assert hub.req("POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user=ADMIN)[1]["state"] == "starting"
    hub.set_print(stg_cur=4)
    hub.app.tick_all()
    assert hub.req("GET", "/api/queue", user=ADMIN)[1]["jobs"][0]["state"] == "printing"
    hub.set_print(gcode_state="FINISH")
    hub.app.tick_all()
    return job["id"]


def test_done_takes_grams_off_the_spool_and_prices_the_job(hub):
    spool = hub.cloud_spool(type="PETG", colour="ADB1B2", grams_left=500, printer=SERIAL, unit=0, slot=0)
    other = hub.cloud_spool(type="PETG", colour="ADB1B2", grams_left=900, printer=SERIAL, unit=0, slot=3)
    hub.app.prices.set(spool["id"], 30)
    jid = finish_a_print(hub)
    done = hub.req("GET", "/api/queue", user=ADMIN)[1]["history"][0]
    assert done["id"] == jid and done["state"] == "done"
    assert done["cost"] == 2.38 and done["spools_used"] == [{"spool_id": spool["id"], "grams": 75.22}]
    assert put_calls(hub) == [("/api/filament/%s?account=a_x" % spool["id"], {"grams_left": 424.78})]  # the other tray's spool is untouched
    assert not os.path.exists(os.path.join(hub.s.SHARE, done["file"]))
    hub.app.tick_all()  # a later tick must not take the grams off again
    assert len(put_calls(hub)) == 1
    assert other["grams_left"] == 900


def test_done_without_a_matching_spool_still_finishes(hub):
    finish_a_print(hub)
    done = hub.req("GET", "/api/queue", user=ADMIN)[1]["history"][0]
    assert done["state"] == "done" and done["spools_used"] == [] and done["cost"] == 0.12  # electricity only: 0.124 at 0.40
    hub.cloud_spool(type="PETG", colour="ADB1B2", grams_left=10, printer=SERIAL, unit=0, slot=0)
    hub.now += 31  # past the cached empty library
    hub.app.settle(dict(done, file="gone.gcode.3mf"))
    assert put_calls(hub)[-1][1] == {"grams_left": 0}  # never below zero


def test_done_settles_when_the_cloud_is_down(hub):
    hub.cloud_spool(type="PETG", colour="ADB1B2", grams_left=500, printer=SERIAL, unit=0, slot=0)
    FakeBridge.library_fail = (502, "cloud down")
    finish_a_print(hub)
    done = hub.req("GET", "/api/queue", user=ADMIN)[1]["history"][0]
    assert done["state"] == "done" and done["spools_used"] == [] and done["cost"] == 0.12


def test_failed_print_keeps_the_spool(hub):
    hub.cloud_spool(type="PETG", colour="ADB1B2", grams_left=500, printer=SERIAL, unit=0, slot=0)
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    jid = hub.sliced(owner=USER)
    hub.req("POST", "/api/jobs/%s/queue" % jid, {"target": SERIAL}, user=USER)
    hub.set_print(gcode_state="IDLE")
    hub.req("POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, user=ADMIN)
    hub.set_print(stg_cur=4)
    hub.app.tick_all()
    hub.set_print(gcode_state="FAILED")
    hub.app.tick_all()
    assert put_calls(hub) == []


def test_spool_without_price_is_allowed_and_costs_nothing(hub):
    hub.setup_printers()
    sp = hub.req("POST", "/api/spools", {"type": "PLA", "name": "PLA Basic", "colour": "ffffff", "price_per_kg": None}, user=ADMIN)
    assert sp[0] == 200 and sp[1]["price_per_kg"] is None
    assert hub.req("PUT", "/api/spools/" + sp[1]["id"], {"price_per_kg": 12.5}, user=ADMIN)[1]["price_per_kg"] == 12.5
    assert hub.req("PUT", "/api/spools/" + sp[1]["id"], {"price_per_kg": None}, user=ADMIN)[1]["price_per_kg"] is None
