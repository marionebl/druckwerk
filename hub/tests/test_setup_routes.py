"""Setup wizard routes: bridge proxying, prefix suggestion from the device registry, printer config, integration secret."""
from hubfakes import ADMIN, SERIAL, SERIAL2, FakeBridge


def test_setup_lists_accounts_and_suggests_prefix_from_device_registry(hub):
    st, body = hub.req("GET", "/api/setup", user=ADMIN)
    assert st == 200
    assert body["accounts"] == [{"id": "a_x", "email": "a@example.invalid", "logged_in": True, "daemon_running": True}]
    by = {p["serial"]: p for p in body["printers"]}
    # The serial's device has an entity registered as sensor.lab_h2s_print_status: the prefix is read from it, not guessed.
    assert by[SERIAL]["suggested_prefix"] == "lab_h2s"
    assert by[SERIAL2]["suggested_prefix"] is None
    assert by[SERIAL]["model"] == "H2S" and by[SERIAL]["device_name"] == "Lab" and by[SERIAL]["configured"] is True
    # SERIAL registered itself on this call (it has a device and a status sensor in HA); SERIAL2 has none.
    assert by[SERIAL2]["configured"] is False


def test_suggested_prefix_falls_back_to_device_name(hub):
    hub.ha.entities = []
    prefix = hub.req("GET", "/api/setup", user=ADMIN)[1]["printers"][0]["suggested_prefix"]
    assert prefix == "lab_h2s"  # name_by_user "Lab H2S" slugged


def test_login_is_proxied_to_the_bridge(hub):
    st, body = hub.req("POST", "/api/setup/login/start", {"email": "a@example.invalid", "password": "pw"}, user=ADMIN)
    assert (st, body["state"]) == (200, "need_code")
    assert hub.req("POST", "/api/setup/login/code", {"email": "a@example.invalid", "code": "123456"}, user=ADMIN)[1]["state"] == "done"
    assert FakeBridge.log[0] == ("/api/login/start", {"email": "a@example.invalid", "password": "pw"})
    assert hub.req("DELETE", "/api/setup/accounts/a_x", user=ADMIN)[1] == {"deleted": "a_x"}


def test_put_and_delete_printer(hub):
    body = {"name": "Lab", "ha_prefix": "lab_h2s", "plate": "High Temp Plate", "nozzle": "0.4",
            "pause_entities": ["automation.snap"], "electricity_kwh_price": 0.3}
    st, p = hub.req("PUT", "/api/setup/printers/" + SERIAL, body, user=ADMIN)
    assert st == 200 and p["model"] == "H2S" and p["ha_prefix"] == "lab_h2s" and p["account"] == "a_x"
    assert hub.app.registry.get(SERIAL)["electricity_kwh_price"] == 0.3
    st, setup = hub.req("GET", "/api/setup", user=ADMIN)
    assert setup["printers"][0]["configured"] and setup["printers"][0]["config"]["plate"] == "High Temp Plate"
    assert hub.req("DELETE", "/api/setup/printers/" + SERIAL, user=ADMIN)[0] == 200
    assert hub.req("DELETE", "/api/setup/printers/" + SERIAL, user=ADMIN)[0] == 404


def test_put_printer_rejects_unknown_prefix_and_serial(hub):
    st, body = hub.req("PUT", "/api/setup/printers/" + SERIAL, {"name": "x", "ha_prefix": "nope"}, user=ADMIN)
    assert st == 400 and "sensor.nope_print_status" in body["error"]
    assert hub.req("PUT", "/api/setup/printers/" + SERIAL, {"name": "x"}, user=ADMIN)[0] == 400
    assert hub.req("PUT", "/api/setup/printers/NOPE", {"name": "x", "ha_prefix": "lab_h2s"}, user=ADMIN)[0] == 404


def test_integration_secret_and_discovery(hub):
    st, body = hub.req("GET", "/api/integration", user=ADMIN)
    assert st == 200 and body["url"] == "http://hub-host:8099" and len(body["secret"]) >= 32
    assert body["secret"] == hub.app.secret
    d = hub.app.discovery()
    assert d == {"service": "druckwerk", "config": {"host": "hub-host", "bridge_port": hub.bridge.server_address[1],
                                                       "hub_port": 8099, "secret": body["secret"]}}


def test_integration_reports_whether_ha_has_a_druckwerk_entry(hub):
    assert hub.req("GET", "/api/integration", user=ADMIN)[1]["connected"] is False
    hub.ha.entries = [{"domain": "druckwerk", "entry_id": "e1"}, {"domain": "other", "entry_id": "e2"}]
    assert hub.req("GET", "/api/integration", user=ADMIN)[1]["connected"] is True
    hub.ha.entries = [{"domain": "other", "entry_id": "e2"}]
    assert hub.req("GET", "/api/integration", user=ADMIN)[1]["connected"] is False
    hub.ha.entries = [{"domain": "druckwerk", "entry_id": "e1"}]
    hub.ha.fail["config_entries/get"] = True  # a websocket error must not break the setup page
    st, body = hub.req("GET", "/api/integration", user=ADMIN)
    assert st == 200 and body["connected"] is False


def test_printers_with_an_ha_device_register_themselves(hub):
    assert hub.req("GET", "/api/printers", user=ADMIN)[1] == []
    hub.req("GET", "/api/setup", user=ADMIN)
    st, printers = hub.req("GET", "/api/printers", user=ADMIN)
    # SERIAL2 has no device in HA, so it is not registered.
    assert [p["serial"] for p in printers] == [SERIAL]
    assert hub.app.registry.get(SERIAL) == {"serial": SERIAL, "name": "Lab", "model": "H2S", "account": "a_x", "prefix": "lab_h2s",
                                            "plate_type": "", "nozzle": "", "pause_entities": [], "electricity_kwh_price": ""}
    assert hub.app.config_view(hub.app.registry.get(SERIAL))["electricity_kwh_price"] == 0  # the registry stores 0 as ""
    assert hub.req("GET", "/api/me", user=ADMIN)[1]["setup_done"] is True


def test_auto_register_needs_the_status_sensor_in_ha(hub):
    del hub.ha.s["sensor.lab_h2s_print_status"]
    hub.req("GET", "/api/setup", user=ADMIN)
    assert hub.app.registry.all() == []


def test_removed_printer_stays_removed_until_saved_again(hub):
    hub.req("GET", "/api/setup", user=ADMIN)
    assert hub.req("DELETE", "/api/setup/printers/" + SERIAL, user=ADMIN)[0] == 200
    hub.req("GET", "/api/setup", user=ADMIN)
    hub.app.tick_all()
    assert hub.app.registry.all() == []
    # The mark survives a restart: it lives in the data dir.
    from queue_registry import Registry
    assert Registry(str(hub.tmp / "data")).is_removed(SERIAL)
    assert hub.req("PUT", "/api/setup/printers/" + SERIAL, {"name": "Lab", "ha_prefix": "lab_h2s"}, user=ADMIN)[0] == 200
    hub.req("DELETE", "/api/setup/printers/" + SERIAL, user=ADMIN)
    assert hub.app.registry.is_removed(SERIAL)
    hub.req("PUT", "/api/setup/printers/" + SERIAL, {"name": "Lab", "ha_prefix": "lab_h2s"}, user=ADMIN)
    assert not hub.app.registry.is_removed(SERIAL)


def test_ticker_registers_at_most_every_60_seconds_and_survives_errors(hub):
    hub.app.tick_all()
    assert [p["serial"] for p in hub.app.registry.all()] == [SERIAL]
    hub.app.registry.remove(SERIAL)
    hub.now += 30
    hub.app.tick_all()
    assert hub.app.registry.all() == []
    hub.now += 31
    hub.app.tick_all()
    assert len(hub.app.registry.all()) == 1
    hub.app.registry.remove(SERIAL)
    hub.now += 61
    hub.app.bridge = ("127.0.0.1", 1)  # unreachable bridge
    hub.app.tick_all()  # must not raise
    assert hub.app.registry.all() == []
