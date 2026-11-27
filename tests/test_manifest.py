"""Thin integration: actions and LLM tools come from the app's manifest and are forwarded."""

import copy

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import llm
from homeassistant.helpers.service import SERVICE_DESCRIPTION_CACHE
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.druckwerk import manifest as manifest_module
from custom_components.druckwerk.llm_api import API_ID

from .conftest import HUB_SECRET, HUB_URL, SERIAL, URL
from .test_hub import MANIFEST, hub_setup, mock_hub, tick

UID = "user-anna"
HEX = "0123456789ab"


def post_calls(mock, suffix):
    return [c for c in mock.mock_calls if c[0] == "POST" and str(c[1]) == f"{HUB_URL}{suffix}"]


def act_as(call):
    return (call[3] or {}).get("X-Druckwerk-Act-As")


async def act(hass, service, data=None, user=UID, response=True):
    return await hass.services.async_call(
        "druckwerk", service, data or {}, blocking=True, return_response=response,
        context=Context(user_id=user) if user else Context(),
    )


def with_actions(**changes):
    """The fixture manifest with another revision; a value of None drops that action."""
    doc = copy.deepcopy(MANIFEST)
    doc["revision"] = "changed000001"
    doc["actions"] = [a for a in doc["actions"] if changes.get(a["name"], 1) is not None]
    for name, action in changes.items():
        if action:
            doc["actions"].append(dict(action, name=name))
    return doc


def llm_context(user=UID):
    return llm.LLMContext(platform="test", context=Context(user_id=user) if user else None, language="en", assistant="conversation", device_id=None)


def issues(hass):
    return {i for (_, i) in ir.async_get(hass).issues}


@pytest.fixture(autouse=True)
def _conversation_stub(monkeypatch):
    """APIInstance.async_call_tool imports the conversation component for tracing, which needs hassil; stub it when absent."""
    try:
        import homeassistant.components.conversation  # noqa: F401
    except ImportError:
        import enum
        import sys
        import types
        stub = types.ModuleType("homeassistant.components.conversation")
        stub.ConversationTraceEventType = enum.Enum("ConversationTraceEventType", "TOOL_CALL")
        stub.async_conversation_trace_append = lambda *a, **k: None
        monkeypatch.setitem(sys.modules, "homeassistant.components.conversation", stub)


@pytest.fixture
async def hub(hass, addon, media, vendor_device):
    await hub_setup(hass, mock=addon)
    return addon


async def test_actions_registered_from_the_manifest(hass, hub):
    services = hass.services.async_services_for_domain("druckwerk")
    assert {a["name"] for a in MANIFEST["actions"]} <= set(services)
    modes = {"none": SupportsResponse.NONE, "optional": SupportsResponse.OPTIONAL, "only": SupportsResponse.ONLY}
    for action in MANIFEST["actions"]:
        assert services[action["name"]].supports_response is modes[action["response"]], action["name"]
    cache = hass.data[SERVICE_DESCRIPTION_CACHE]
    desc = cache[("druckwerk", "start_next")]
    assert "camera snapshot" in desc["description"]
    assert desc["fields"]["printer"]["required"] is True and "required" not in desc["fields"]["plate_clear"]
    assert desc["fields"]["plate_clear"]["default"] is False and desc["fields"]["printer"]["selector"]


async def test_local_print_file_wins_over_the_manifest(hass, hub):
    assert ("druckwerk", "print_file") not in hass.data[SERVICE_DESCRIPTION_CACHE]
    with pytest.raises(Exception, match="device_id|serial"):  # the local schema runs, not the permissive one
        await act(hass, "print_file", {"file": "a.gcode.3mf"})


async def test_cached_manifest_is_used_when_the_app_is_down(hass, addon, hass_storage, media, vendor_device):
    hass_storage["druckwerk.manifest.e1"] = {"version": 1, "data": MANIFEST}
    addon.get(f"{HUB_URL}/api/events", exc=aiohttp.ClientError)
    entry = MockConfigEntry(entry_id="e1", domain="druckwerk", unique_id="druckwerk", data={"url": URL, "hub_secret": HUB_SECRET})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert "get_queue" in hass.services.async_services_for_domain("druckwerk")
    assert API_ID in [a.id for a in llm.async_get_apis(hass)]


async def test_good_manifest_is_cached(hass, addon, hass_storage, media, vendor_device):
    mock_hub(addon)
    entry = MockConfigEntry(entry_id="e1", domain="druckwerk", unique_id="druckwerk", data={"url": URL, "hub_secret": HUB_SECRET})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage["druckwerk.manifest.e1"]["data"]["revision"] == MANIFEST["revision"]


async def test_new_revision_changes_actions_and_tools_without_reload(hass, hub):
    (entry,) = hass.config_entries.async_entries("druckwerk")
    runtime = entry.runtime_data
    new_action = {"description": "New.", "response": "none", "serial_fields": [], "fields": {}}
    doc = with_actions(skip_job=None, wave=new_action)
    doc["llm"]["tools"] = [t for t in doc["llm"]["tools"] if t["name"] != "druckwerk_skip_job"]
    mock_hub(hub, manifest=doc)
    await tick(hass)
    services = hass.services.async_services_for_domain("druckwerk")
    assert "skip_job" not in services and "wave" in services and "get_queue" in services
    assert ("druckwerk", "wave") in hass.data[SERVICE_DESCRIPTION_CACHE]
    inst = await llm.async_get_api(hass, API_ID, llm_context())
    assert "druckwerk_skip_job" not in {t.name for t in inst.tools}
    assert entry.state is ConfigEntryState.LOADED and entry.runtime_data is runtime
    assert len([a for a in llm.async_get_apis(hass) if a.id == API_ID]) == 1


async def test_same_revision_is_not_applied_again(hass, hub):
    before = hass.services.async_services_for_domain("druckwerk")["get_queue"]
    await tick(hass)
    assert hass.services.async_services_for_domain("druckwerk")["get_queue"] is before


async def test_device_id_becomes_the_serial_in_serial_fields(hass, hub, vendor_device):
    hub.post(f"{HUB_URL}/api/actions/start_next", json={"result": {"job": {"id": "a" * 12}}})
    res = await act(hass, "start_next", {"printer": vendor_device.id, "plate_clear": True})
    assert res == {"job": {"id": "a" * 12}}
    (post,) = post_calls(hub, "/api/actions/start_next")
    assert post[2] == {"printer": SERIAL, "plate_clear": True}


async def test_other_values_stay_and_other_fields_are_not_replaced(hass, hub, vendor_device):
    hub.post(f"{HUB_URL}/api/actions/cancel_job", json={"result": {}})
    await act(hass, "cancel_job", {"job_id": vendor_device.id})
    assert post_calls(hub, "/api/actions/cancel_job")[0][2] == {"job_id": vendor_device.id}


async def test_act_as_header_follows_the_caller(hass, hub):
    hub.post(f"{HUB_URL}/api/actions/get_queue", json={"result": {"waiting": {}}})
    await act(hass, "get_queue")
    await act(hass, "get_queue", user=None)
    assert [act_as(c) for c in post_calls(hub, "/api/actions/get_queue")] == [UID, None]


async def test_only_action_needs_return_response_and_none_returns_nothing(hass, hub):
    hub.post(f"{HUB_URL}/api/actions/get_queue", json={"result": {}})
    with pytest.raises(ServiceValidationError):
        await act(hass, "get_queue", response=False)
    doc = with_actions(ping={"description": "Ping.", "response": "none", "serial_fields": [], "fields": {}})
    mock_hub(hub, manifest=doc)
    hub.post(f"{HUB_URL}/api/actions/ping", json={"result": {}})
    await tick(hass)
    assert await act(hass, "ping", response=False) is None


async def test_hub_400_is_a_validation_error_and_403_keeps_its_text(hass, hub):
    hub.post(f"{HUB_URL}/api/actions/cancel_job", json={"error": "job_id must be 12 hex characters"}, status=400)
    with pytest.raises(ServiceValidationError, match="12 hex"):
        await act(hass, "cancel_job", {"job_id": "x"})
    hub._mocks.clear()
    mock_hub(hub)
    hub.post(f"{HUB_URL}/api/actions/cancel_job", json={"error": "rule: only admins cancel"}, status=403)
    with pytest.raises(HomeAssistantError, match="rule: only admins") as err:
        await act(hass, "cancel_job", {"job_id": HEX})
    assert not isinstance(err.value, ServiceValidationError)


async def test_unreachable_app_gives_a_clear_error(hass, hub):
    hub.post(f"{HUB_URL}/api/actions/cancel_job", exc=aiohttp.ClientError("down"))
    with pytest.raises(HomeAssistantError, match="not reachable"):
        await act(hass, "cancel_job", {"job_id": HEX})


# ---- LLM API
async def test_llm_api_from_the_manifest(hass, hub):
    inst = await llm.async_get_api(hass, API_ID, llm_context())
    assert {t.name for t in inst.tools} == {t["name"] for t in MANIFEST["llm"]["tools"]}
    assert inst.api_prompt == MANIFEST["llm"]["prompt"]
    by = {t.name: t for t in inst.tools}
    assert by["druckwerk_get_printers"].annotations.read_only and not by["druckwerk_start_next"].annotations.read_only
    assert all(t.integration == "druckwerk" and t.description for t in inst.tools)


async def test_llm_tools_serialise_for_the_mcp_server(hass, hub):
    import probatio

    inst = await llm.async_get_api(hass, API_ID, llm_context())
    for tool in inst.tools:
        schema = probatio.to_openapi(tool.parameters, custom_serializer=inst.custom_serializer, openapi_version="3.1.0")
        spec = next(t for t in MANIFEST["llm"]["tools"] if t["name"] == tool.name)["parameters"]
        assert set(schema["properties"]) == set(spec["properties"]), tool.name
        assert sorted(schema.get("required", [])) == sorted(spec.get("required", [])), tool.name
        assert all("type" in p or "anyOf" in p for p in schema["properties"].values())
    add = next(t for t in inst.tools if t.name == "druckwerk_add_to_queue")
    options = probatio.to_openapi(add.parameters, openapi_version="3.1.0")["properties"]["options"]
    assert "bed_type" in options["properties"]


async def test_llm_tool_forwards_as_the_context_user(hass, hub):
    hub.post(f"{HUB_URL}/api/tools/druckwerk_cancel_job", json={"result": {"job": {"state": "cancelled"}}})
    inst = await llm.async_get_api(hass, API_ID, llm_context("user-ben"))
    res = await inst.async_call_tool(llm.ToolInput("druckwerk_cancel_job", {"job_id": HEX}))
    assert res.data == {"job": {"state": "cancelled"}} and not res.error
    (post,) = post_calls(hub, "/api/tools/druckwerk_cancel_job")
    assert act_as(post) == "user-ben" and post[2] == {"job_id": HEX}


async def test_llm_tool_without_user_is_internal_and_400_raises(hass, hub):
    hub.post(f"{HUB_URL}/api/tools/druckwerk_get_queue", json={"result": {}})
    inst = await llm.async_get_api(hass, API_ID, llm_context(None))
    await inst.async_call_tool(llm.ToolInput("druckwerk_get_queue", {}))
    assert act_as(post_calls(hub, "/api/tools/druckwerk_get_queue")[0]) is None
    hub.post(f"{HUB_URL}/api/tools/druckwerk_cancel_job", json={"error": "job_id is required"}, status=400)
    with pytest.raises(ServiceValidationError, match="job_id"):
        await inst.async_call_tool(llm.ToolInput("druckwerk_cancel_job", {}))


async def test_unload_removes_actions_and_api_and_setup_brings_them_back(hass, hub):
    (entry,) = hass.config_entries.async_entries("druckwerk")
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert "get_queue" not in hass.services.async_services_for_domain("druckwerk")
    assert "print_file" in hass.services.async_services_for_domain("druckwerk")
    assert API_ID not in [a.id for a in llm.async_get_apis(hass)]
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert "get_queue" in hass.services.async_services_for_domain("druckwerk")
    assert API_ID in [a.id for a in llm.async_get_apis(hass)]


async def test_without_hub_secret_nothing_is_registered(hass, addon, media, vendor_device):
    entry = MockConfigEntry(domain="druckwerk", unique_id="druckwerk", data={"url": URL})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert API_ID not in [a.id for a in llm.async_get_apis(hass)]
    assert "get_queue" not in hass.services.async_services_for_domain("druckwerk")


# ---- version negotiation
async def test_newer_api_version_raises_an_issue_and_registers_nothing(hass, addon, media, vendor_device):
    await hub_setup(hass, mock=addon, manifest=dict(MANIFEST, api_version=2))
    assert "get_queue" not in hass.services.async_services_for_domain("druckwerk")
    assert API_ID not in [a.id for a in llm.async_get_apis(hass)]
    assert issues(hass) == {"integration_too_old"}
    mock_hub(addon, manifest=dict(MANIFEST, api_version=1, revision="again0000001"))
    await tick(hass)
    assert issues(hass) == set() and "get_queue" in hass.services.async_services_for_domain("druckwerk")


async def setup_with_missing_route(hass, addon):
    mock_hub(addon)
    addon.get(f"{HUB_URL}/api/integration/manifest", status=404)
    addon._mocks.insert(0, addon._mocks.pop())  # the first match is served
    entry = MockConfigEntry(entry_id="e1", domain="druckwerk", unique_id="druckwerk", data={"url": URL, "hub_secret": HUB_SECRET})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_no_manifest_route_and_no_cache_raises_an_issue(hass, addon, media, vendor_device):
    await setup_with_missing_route(hass, addon)
    assert issues(hass) == {"app_too_old"}
    assert "get_queue" not in hass.services.async_services_for_domain("druckwerk")


async def test_no_manifest_route_keeps_the_cache_and_warns_once(hass, addon, hass_storage, media, vendor_device, monkeypatch):
    warnings = []
    monkeypatch.setattr(manifest_module._LOGGER, "warning", lambda *a: warnings.append(a))
    hass_storage["druckwerk.manifest.e1"] = {"version": 1, "data": MANIFEST}
    await setup_with_missing_route(hass, addon)
    await tick(hass)
    assert issues(hass) == set() and "get_queue" in hass.services.async_services_for_domain("druckwerk")
    assert len(warnings) == 1
