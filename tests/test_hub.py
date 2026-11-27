"""Config flow, discovery, options flow and the options push to the app."""

import aiohttp
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .conftest import HUB_SECRET, HUB_URL, MANIFEST, URL, calls, mock_hub, setup_entry, state_doc, tick

HUBHOST = "c9a833d4-druckwerk"
RECIP = {"person.anna": "notify.mobile_app_anna"}
OPTIONS = f"{HUB_URL}/api/integration/options"


# ---- config flow
async def test_flow_ok(hass, app):
    r = await hass.config_entries.flow.async_init("druckwerk", context={"source": config_entries.SOURCE_USER})
    assert r["type"] == "form"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"url": URL + "/"})
    assert r["type"] == "create_entry" and r["data"] == {"url": URL}
    r2 = await hass.config_entries.flow.async_init("druckwerk", context={"source": config_entries.SOURCE_USER})
    assert r2["type"] == "abort"  # single instance


async def test_flow_cannot_connect(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/api/health", exc=aiohttp.ClientError)
    r = await hass.config_entries.flow.async_init("druckwerk", context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"url": URL})
    assert r["type"] == "form" and r["errors"] == {"base": "cannot_connect"}


async def test_flow_default_url_from_supervisor(hass, app, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    app.get("http://supervisor/addons", json={"data": {"addons": [{"slug": "core_ssh"}, {"slug": "c9a833d4_druckwerk"}]}})
    app.get("http://supervisor/addons/c9a833d4_druckwerk/info", json={"data": {"hostname": HUBHOST}})
    r = await hass.config_entries.flow.async_init("druckwerk", context={"source": "user"})
    assert r["data_schema"]({}) == {"url": f"http://{HUBHOST}:8098"}


async def test_flow_default_without_supervisor_is_the_computed_host(hass, app, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    r = await hass.config_entries.flow.async_init("druckwerk", context={"source": "user"})
    assert r["data_schema"]({}) == {"url": f"http://{HUBHOST}:8098"}


# ---- Supervisor discovery
def hassio_info(host=HUBHOST, secret=HUB_SECRET):
    cfg = {"host": host, "bridge_port": 8098, "hub_port": 8099, "secret": secret}
    return HassioServiceInfo(config=cfg, name="Druckwerk", slug="0990873a_druckwerk", uuid="abc")


async def test_discovery_creates_entry(hass, app):
    r = await hass.config_entries.flow.async_init("druckwerk", context={"source": "hassio"}, data=hassio_info())
    assert r["type"] == "create_entry" and r["title"] == "Druckwerk"
    assert r["data"] == {"url": f"http://{HUBHOST}:8098", "hub_url": f"http://{HUBHOST}:8099", "hub_secret": HUB_SECRET}


async def test_discovery_updates_the_existing_entry_and_keeps_entity_ids(hass, app, vendor_device):
    entry = await setup_entry(hass)
    reg = er.async_get(hass)
    ids = {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(reg, entry.entry_id)}
    hub = f"http://{HUBHOST}:8099"
    app.get(f"{hub}/api/integration/state", json=dict(state_doc(), manifest_revision=MANIFEST["revision"]))
    app.put(f"{hub}/api/integration/options", json={"recipients": {}})
    new = "n" * 32
    r = await hass.config_entries.flow.async_init("druckwerk", context={"source": "hassio"}, data=hassio_info(secret=new))
    assert r["type"] == "abort" and r["reason"] == "already_configured"
    await hass.async_block_till_done()
    assert entry.data == {"url": f"http://{HUBHOST}:8098", "hub_url": hub, "hub_secret": new}
    assert entry.runtime_data.url == hub and entry.runtime_data.secret == new  # reloaded
    after = {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(reg, entry.entry_id) if e.unique_id in ids}
    assert after == ids and ids


# ---- options flow
async def test_options_manual_secret_rejected_then_accepted(hass, app, vendor_device):
    entry = await setup_entry(hass, data={"url": URL})
    app.get(f"{HUB_URL}/api/integration/manifest", status=401, json={"error": "no"})
    app._mocks.insert(0, app._mocks.pop())
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    bad = await hass.config_entries.options.async_configure(flow["flow_id"], {"hub_secret": "nope"})
    assert bad["errors"] == {"hub_secret": "invalid_hub_secret"}
    mock_hub(app)
    ok = await hass.config_entries.options.async_configure(flow["flow_id"], {"hub_secret": HUB_SECRET, "recipients": RECIP})
    assert ok["type"] == "create_entry"
    assert entry.data["hub_secret"] == HUB_SECRET and entry.options["recipients"] == RECIP


async def test_options_rejects_bad_recipients_and_a_short_upload_secret(hass, app, vendor_device):
    entry = await setup_entry(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(flow["flow_id"], {"recipients": {"light.x": "notify.y"}})
    assert r["errors"] == {"recipients": "bad_recipients"}
    short = await hass.config_entries.options.async_configure(flow["flow_id"], {"upload_secret": "short"})
    assert short["errors"] == {"upload_secret": "secret_too_short"}


async def test_options_flow_sets_the_upload_secret(hass, app, vendor_device):
    entry = await setup_entry(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    done = await hass.config_entries.options.async_configure(flow["flow_id"], {"upload_secret": "u" * 32})
    assert done["type"] == "create_entry" and entry.options == {"upload_secret": "u" * 32}


async def test_without_a_hub_secret_nothing_is_polled_or_created(hass, app, vendor_device):
    entry = await setup_entry(hass, data={"url": URL})
    await tick(hass)
    assert entry.state is ConfigEntryState.LOADED and not er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert not calls(app, "GET", "/api/integration/state")


# ---- options push
async def test_recipients_are_pushed_at_setup(hass, app, vendor_device):
    await setup_entry(hass, options={"recipients": RECIP})
    (put,) = calls(app, "PUT", "/api/integration/options")
    assert put[2] == {"recipients": RECIP} and put[3]["X-Hub-Secret"] == HUB_SECRET


async def test_without_recipients_an_empty_object_is_pushed(hass, app, vendor_device):
    await setup_entry(hass)
    assert calls(app, "PUT", "/api/integration/options")[0][2] == {"recipients": {}}


async def test_a_change_of_the_options_is_pushed_without_a_reload(hass, app, vendor_device):
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    hass.config_entries.async_update_entry(entry, options={"recipients": RECIP, "upload_secret": "u" * 32})
    await hass.async_block_till_done()
    assert [c[2] for c in calls(app, "PUT", "/api/integration/options")] == [{"recipients": {}}, {"recipients": RECIP}]
    assert entry.runtime_data is runtime and entry.state is ConfigEntryState.LOADED


async def test_the_options_are_pushed_again_when_the_app_answers_after_being_down(hass, app, vendor_device):
    await setup_entry(hass, options={"recipients": RECIP})
    await tick(hass)
    assert len(calls(app, "PUT", "/api/integration/options")) == 1  # not on every poll
    app.get(f"{HUB_URL}/api/integration/state", exc=aiohttp.ClientError)
    app._mocks.insert(0, app._mocks.pop())
    await tick(hass)
    mock_hub(app)
    await tick(hass, 12)
    assert [c[2] for c in calls(app, "PUT", "/api/integration/options")] == [{"recipients": RECIP}] * 2
