"""Actions and LLM API for agents: mocked hub, act-as header, confirmation rule, response support."""

import json
import os
from pathlib import Path

import pytest
from homeassistant.core import Context, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.druckwerk import agent
from custom_components.druckwerk.llm_api import API_ID, TOOLS

from .conftest import HUB_URL, SERIAL
from .test_hub import hub_setup

UID = "user-anna"
CTX = Context(user_id=UID)
PRINTERS = [{"serial": SERIAL, "name": "Lab H2S", "model": "H2S", "role": "admin", "status": "idle", "progress": 0,
             "ams": [{"unit": 0, "trays": [{"slot": 0, "type": "PETG"}]}], "queue_length": 1}]
JOB = {"id": "aaaaaaaaaaaa", "file": "cube.gcode.3mf", "state": "queued", "target": SERIAL, "owner_name": "Anna"}
HEX = "0123456789ab"


def on(mock, method, path, json=None, status=200, **kw):  # kw: content=bytes for images
    """Register a hub mock that wins over the earlier ones (the first match is served)."""
    getattr(mock, method)(f"{HUB_URL}{path}", json=json, status=status, **kw)
    mock._mocks.insert(0, mock._mocks.pop())


def hub_calls(mock, method=None, suffix=None):
    return [c for c in mock.mock_calls if str(c[1]).startswith(HUB_URL) and (method is None or c[0] == method.upper())
            and (suffix is None or str(c[1]).endswith(suffix))]


def act_as(call):
    return (call[3] or {}).get("X-Druckwerk-Act-As")


async def act(hass, service, data=None, user=UID, response=True):
    return await hass.services.async_call(
        "druckwerk", service, data or {}, blocking=True, return_response=response, context=Context(user_id=user) if user else Context()
    )


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
    on(addon, "get", "/api/printers", PRINTERS)
    return addon


# ---- reads
async def test_get_printers_only_response_and_act_as(hass, hub):
    res = await act(hass, "get_printers")
    assert res["printers"][0]["serial"] == SERIAL and res["printers"][0]["ams"][0]["trays"][0]["type"] == "PETG"
    assert {act_as(c) for c in hub_calls(hub, "get", "/api/printers")} == {UID}
    with pytest.raises(ServiceValidationError):  # SupportsResponse.ONLY: a call without return_response is refused
        await act(hass, "get_printers", response=False)


async def test_get_printers_filter_by_name_and_unknown(hass, hub):
    assert (await act(hass, "get_printers", {"printer": "lab h2s"}))["printers"][0]["serial"] == SERIAL
    with pytest.raises(ServiceValidationError, match="No printer"):
        await act(hass, "get_printers", {"printer": "nope"})


async def test_no_user_means_no_act_as_header(hass, hub):
    await act(hass, "get_printers", user=None)
    assert all(act_as(c) is None for c in hub_calls(hub, "get", "/api/printers"))


async def test_get_queue_groups_waiting_jobs(hass, hub):
    jobs = [dict(JOB, id="a" * 12, name="Benchy for Anna", kind="print"), dict(JOB, id="b" * 12, target="any", model="H2S"),
            dict(JOB, id="c" * 12, state="pending_approval"), dict(JOB, id="d" * 12, state="printing", printer=SERIAL)]
    on(hub, "get", "/api/queue", {"jobs": jobs, "history": [dict(JOB, id="e" * 12, state="done"), dict(JOB, id="f" * 12, state="failed")],
                                  "printers": []})
    res = await act(hass, "get_queue", {"history_limit": 1})
    assert [(j["id"][0], j["position"]) for j in res["waiting"][SERIAL]] == [("a", 0), ("c", 1)]
    assert (res["waiting"][SERIAL][0]["name"], res["waiting"][SERIAL][0]["kind"]) == ("Benchy for Anna", "print")
    assert [j["id"][0] for j in res["waiting"]["any"]] == ["b"]
    assert [j["id"][0] for j in res["pending_approvals"]] == ["c"] and [j["id"][0] for j in res["running"]] == ["d"]
    assert [j["id"][0] for j in res["history"]] == ["e"]
    assert act_as(hub_calls(hub, "get", "/api/queue")[-1]) == UID


# ---- add to queue
async def test_add_to_queue_file_and_slice_job(hass, hub):
    on(hub, "post", "/api/queue", JOB)
    on(hub, "post", f"/api/jobs/{HEX}/queue", dict(JOB, id="bbbbbbbbbbbb"))
    res = await act(hass, "add_to_queue", {"file": "cube.gcode.3mf", "target": "Lab H2S", "options": {"timelapse": True}})
    (post,) = hub_calls(hub, "post", "/api/queue")
    assert res["job"]["id"] == "aaaaaaaaaaaa" and act_as(post) == UID
    assert post[2] == {"target": SERIAL, "options": {"timelapse": True}, "file": "cube.gcode.3mf"}
    res = await act(hass, "add_to_queue", {"slice_job_id": HEX})
    (post,) = hub_calls(hub, "post", f"/api/jobs/{HEX}/queue")
    assert post[2] == {"target": "any", "options": {}} and act_as(post) == UID and res["job"]["id"] == "bbbbbbbbbbbb"


async def test_add_to_queue_takes_name_and_advanced_options(hass, hub):
    on(hub, "post", "/api/queue", JOB)
    opts = {"bed_leveling": "auto", "flow_cali": False, "layer_inspect": True, "bed_type": "cool_plate"}
    await act(hass, "add_to_queue", {"file": "cube.gcode.3mf", "target": SERIAL, "name": "Benchy for Anna", "options": opts})
    (post,) = hub_calls(hub, "post", "/api/queue")
    assert post[2] == {"target": SERIAL, "options": opts, "file": "cube.gcode.3mf", "name": "Benchy for Anna"}
    with pytest.raises(Exception):
        await act(hass, "add_to_queue", {"file": "cube.gcode.3mf", "target": SERIAL, "options": {"bed_type": "gold"}})
    with pytest.raises(Exception):
        await act(hass, "add_to_queue", {"file": "cube.gcode.3mf", "target": SERIAL, "name": "x" * 81})


async def test_add_to_queue_waits_for_the_hub_to_copy_the_file(hass, hub, monkeypatch):
    from custom_components.druckwerk import hub_api
    seen, real = [], hub_api.hub_request

    async def spy(*a, **kw):
        seen.append((a[4], kw.get("timeout")))
        return await real(*a, **kw)

    monkeypatch.setattr(hub_api, "hub_request", spy)
    on(hub, "post", "/api/queue", JOB)
    await act(hass, "add_to_queue", {"file": "/media/bambu_sender/plate.gcode.3mf", "target": SERIAL})
    assert ("/api/queue", 120) in seen


async def test_add_to_queue_accepts_an_absolute_media_path(hass, hub):
    on(hub, "post", "/api/queue", JOB)
    await act(hass, "add_to_queue", {"file": "/media/bambu_sender/plate.gcode.3mf", "target": SERIAL})
    assert hub_calls(hub, "post", "/api/queue")[0][2]["file"] == "/media/bambu_sender/plate.gcode.3mf"


async def test_add_to_queue_any_infers_the_only_model(hass, hub):
    on(hub, "post", "/api/queue", JOB)
    await act(hass, "add_to_queue", {"file": "cube.gcode.3mf"})
    assert hub_calls(hub, "post", "/api/queue")[0][2]["model"] == "H2S"


@pytest.mark.parametrize("data", [{}, {"file": "a.gcode.3mf", "slice_job_id": HEX}])
async def test_add_to_queue_needs_exactly_one_source(hass, hub, data):
    with pytest.raises(ServiceValidationError, match="exactly one"):
        await act(hass, "add_to_queue", data)


async def test_hub_refusal_text_reaches_the_caller(hass, hub):
    on(hub, "post", "/api/queue", {"error": "rule: adding a job needs an active grant on the target printer"}, status=403)
    with pytest.raises(HomeAssistantError, match="rule: adding a job needs an active grant"):
        await act(hass, "add_to_queue", {"file": "a.gcode.3mf", "target": SERIAL})


# ---- slicing
async def test_slice_three_variants(hass, hub, media, monkeypatch):
    monkeypatch.setattr(agent, "SLICE_POLL", 0)
    model = media / "cube.stl"
    model.write_bytes(b"solid x")
    ids = iter(["a" * 12, "b" * 12, "c" * 12])
    quality = {}


    from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

    async def up(method, url, data):
        return AiohttpClientMockResponse(method, url, json={"id": next(ids), "state": "uploaded"})
    hub.post(f"{HUB_URL}/api/jobs?name=cube.stl", side_effect=up)
    hub._mocks.insert(0, hub._mocks.pop())
    for jid, q, secs, grams in (("a" * 12, "good", 3600, 10.0), ("b" * 12, "better", 5400, 10.5), ("c" * 12, "best", 9000, 11.0)):
        on(hub, "post", f"/api/jobs/{jid}/slice", {"id": jid, "state": "queued"}, status=202)
        on(hub, "get", f"/api/jobs/{jid}", {"id": jid, "state": "done", "quality": q, "cost_estimate": round(grams * 0.02, 2),
                                            "info": {"seconds": secs, "grams": grams, "has_png": True, "warnings": []},
                                            "settings": {"process": "0.20mm Standard @X"}})
        on(hub, "get", f"/api/jobs/{jid}/plate.png", None, content=b"\x89PNGfake")
    res = await act(hass, "slice", {"model_file": str(model), "printer": SERIAL, "tray": "0:0", "plate": "Textured PEI Plate"})
    assert res["state"] == "done" and res["slice_job_ids"] == {"good": "a" * 12, "better": "b" * 12, "best": "c" * 12}
    assert [(v["quality"], v["print_time"], v["grams"], v["cost"]) for v in res["variants"]] == [
        ("good", "1 h 00 min", 10.0, 0.2), ("better", "1 h 30 min", 10.5, 0.21), ("best", "2 h 30 min", 11.0, 0.22)]
    assert Path(res["variants"][0]["plate_image"]).read_bytes() == b"\x89PNGfake"
    posts = hub_calls(hub, "post", "/slice")
    assert [c[2] for c in posts] == [{"serial": SERIAL, "quality": q, "tray": "0:0", "bed": "Textured PEI Plate"} for q in agent.QUALITIES]
    uploads = hub_calls(hub, "post", "/api/jobs?name=cube.stl")
    assert len(uploads) == 3 and all(act_as(c) == UID for c in uploads + posts)
    assert uploads[0][2] == b"solid x"


async def test_slice_wait_false_returns_ids_and_get_slice_reads_them(hass, hub, media):
    model = media / "druckwerk" / "cube.stl"
    model.write_bytes(b"solid x")
    ids = iter("abc")
    from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

    async def up(m, u, d):
        return AiohttpClientMockResponse(m, u, json={"id": next(ids) * 12})
    hub.post(f"{HUB_URL}/api/jobs?name=cube.stl",
             side_effect=up)
    hub._mocks.insert(0, hub._mocks.pop())
    for c in "abc":
        on(hub, "post", f"/api/jobs/{c * 12}/slice", {}, status=202)
    res = await act(hass, "slice", {"model_file": str(model), "printer": SERIAL, "tray": "0:0", "wait": False})
    assert res["state"] == "slicing" and list(res["slice_job_ids"]) == ["good", "better", "best"]
    on(hub, "get", "/api/jobs/" + "a" * 12, {"id": "a" * 12, "state": "slicing", "quality": "good"})
    got = await act(hass, "get_slice", {"slice_job_ids": ["a" * 12]})
    assert got["state"] == "slicing" and got["variants"][0]["state"] == "slicing"


@pytest.mark.parametrize("path, tray, match", [("/etc/passwd", "0:0", "inside"), ("rel.stl", "0:0", "absolute"), ("MEDIA/x.txt", "0:0", "must be one of"),
                                               ("MEDIA/cube.stl", "bad", "unit:slot")])
async def test_slice_rejects_bad_input(hass, hub, media, path, tray, match):
    (media / "x.txt").write_text("x")
    (media / "cube.stl").write_bytes(b"s")
    with pytest.raises(ServiceValidationError, match=match):
        await act(hass, "slice", {"model_file": path.replace("MEDIA", str(media)), "printer": SERIAL, "tray": tray})
    assert hub_calls(hub, "post") == []


# ---- queue management
@pytest.mark.parametrize("service", ["cancel_job", "skip_job", "approve_job", "reject_job", "retry_job"])
async def test_job_actions_post_with_act_as(hass, hub, service):
    on(hub, "post", f"/api/queue/{HEX}/{service.removesuffix('_job')}", dict(JOB, state="cancelled"))
    res = await act(hass, service, {"job_id": HEX})
    (post,) = hub_calls(hub, "post", f"/{service.removesuffix('_job')}")
    assert res["job"]["id"] == "aaaaaaaaaaaa" and act_as(post) == UID and post[2] == {}
    assert await act(hass, service, {"job_id": HEX}, response=False) is None  # writes: response optional


async def test_reorder_and_move(hass, hub):
    on(hub, "post", f"/api/queue/{HEX}/reorder", JOB)
    on(hub, "post", f"/api/queue/{HEX}/move", JOB)
    await act(hass, "reorder_job", {"job_id": HEX, "position": 2, "printer": "Lab H2S"})
    await act(hass, "move_job", {"job_id": HEX, "target": "any", "model": "H2S"})
    assert hub_calls(hub, "post", "/reorder")[0][2] == {"position": 2, "scope": SERIAL}
    assert hub_calls(hub, "post", "/move")[0][2] == {"target": "any", "model": "H2S"}
    await act(hass, "reorder_job", {"job_id": HEX, "position": 0})
    assert hub_calls(hub, "post", "/reorder")[1][2] == {"position": 0}


async def test_non_admin_refusal_is_reported(hass, hub):
    on(hub, "post", f"/api/queue/{HEX}/skip", {"error": "rule: only an admin of the job's printer can skip a job"}, status=403)
    with pytest.raises(HomeAssistantError, match="rule: only an admin"):
        await act(hass, "skip_job", {"job_id": HEX})


# ---- start needs both confirmations
@pytest.mark.parametrize("flags", [{}, {"plate_clear": True}, {"confirmed_by_person": True}, {"plate_clear": True, "confirmed_by_person": False}])
@pytest.mark.parametrize("service, extra", [("start_next", {}), ("start_job", {"job_id": HEX})])
async def test_start_refused_without_both_flags(hass, hub, service, extra, flags):
    with pytest.raises(ServiceValidationError, match="camera snapshot"):
        await act(hass, service, {"printer": SERIAL, **extra, **flags})
    assert hub_calls(hub, "post", "start-next") == [] and hub_calls(hub, "post", "/start") == []


async def test_start_next_and_start_job_with_both_flags(hass, hub):
    on(hub, "post", f"/api/printers/{SERIAL}/start-next", dict(JOB, state="starting"))
    on(hub, "post", f"/api/printers/{SERIAL}/start", dict(JOB, state="starting"))
    flags = {"plate_clear": True, "confirmed_by_person": True}
    res = await act(hass, "start_next", {"printer": "Lab H2S", **flags})
    assert res["job"]["state"] == "starting"
    await act(hass, "start_job", {"printer": SERIAL, "job_id": HEX, **flags})
    (nxt,) = hub_calls(hub, "post", "start-next")
    (one,) = hub_calls(hub, "post", f"/{SERIAL}/start")
    assert nxt[2] == {"plate_clear": True} and one[2] == {"job_id": HEX, "plate_clear": True}
    assert act_as(nxt) == UID and act_as(one) == UID


# ---- camera
async def test_camera_snapshot_saved_to_media(hass, hub, media):
    on(hub, "get", f"/api/printers/{SERIAL}/camera?mode=snapshot", None, content=b"\xff\xd8JPEG")
    res = await act(hass, "camera_snapshot", {"printer": SERIAL})
    path = Path(res["path"])
    assert path.parent == media / "druckwerk" and path.name.startswith(SERIAL + "-") and path.suffix == ".jpg"
    assert path.read_bytes() == b"\xff\xd8JPEG" and res["media_content_id"].endswith("/druckwerk/" + path.name)
    assert act_as(hub_calls(hub, "get", "mode=snapshot")[0]) == UID
    with pytest.raises(ServiceValidationError):
        await act(hass, "camera_snapshot", {"printer": SERIAL}, response=False)


async def test_snapshots_are_pruned(hass, hub, media):
    for i in range(agent.SNAPSHOT_KEEP + 3):
        (media / "druckwerk" / f"{SERIAL}-2020010{i:02d}.jpg").write_bytes(b"x")
    on(hub, "get", f"/api/printers/{SERIAL}/camera?mode=snapshot", None, content=b"new")
    await act(hass, "camera_snapshot", {"printer": SERIAL})
    assert len([n for n in os.listdir(media / "druckwerk") if n.endswith(".jpg")]) == agent.SNAPSHOT_KEEP


# ---- registration
READS = {"get_printers", "get_queue", "get_slice", "camera_snapshot"}
WRITES = {"add_to_queue", "slice", "cancel_job", "reorder_job", "move_job", "skip_job", "approve_job", "reject_job",
          "retry_job", "start_next", "start_job"}


async def test_response_support_and_descriptions(hass, hub):
    services = hass.services.async_services_for_domain("druckwerk")
    assert READS | WRITES <= set(services)
    for name in READS:
        assert services[name].supports_response is SupportsResponse.ONLY, name
    for name in WRITES:
        assert services[name].supports_response is SupportsResponse.OPTIONAL, name
    root = Path(agent.__file__).parent
    yaml_text = (root / "services.yaml").read_text()
    for fname in ("strings.json", "translations/en.json"):
        strings = json.loads((root / fname).read_text())["services"]
        for name in READS | WRITES:
            assert len(strings[name]["description"]) > 40, (fname, name)
            assert all(f["description"] for f in strings[name]["fields"].values())
    import yaml
    spec = yaml.safe_load(yaml_text)
    for name in READS | WRITES:
        assert name in spec
        for field in json.loads((root / "strings.json").read_text())["services"][name]["fields"]:
            assert "selector" in spec[name]["fields"][field], (name, field)
    for name in ("start_next", "start_job"):
        text = json.loads((root / "strings.json").read_text())["services"][name]["description"]
        assert "camera snapshot" in text and "plate review" in text and "explicit yes" in text


async def test_print_file_is_unchanged(hass, hub):
    assert "print_file" in hass.services.async_services_for_domain("druckwerk")


# ---- LLM API
def llm_context(user=UID):
    return llm.LLMContext(platform="test", context=Context(user_id=user) if user else None, language="en", assistant="conversation", device_id=None)


async def test_llm_api_registered_with_tools_and_prompt(hass, hub):
    assert API_ID in [a.id for a in llm.async_get_apis(hass)]
    inst = await llm.async_get_api(hass, API_ID, llm_context())
    names = {t.name for t in inst.tools}
    assert names == {"druckwerk_" + n for n in (READS | WRITES) - {"get_slice"}} | {"druckwerk_get_slice_results"}
    assert len(names) == len(TOOLS) and all(t.integration == "druckwerk" and t.description for t in inst.tools)
    for needle in ("plate_clear", "camera snapshot", "minutes", "rule:", "admins"):
        assert needle in inst.api_prompt
    by = {t.name: t for t in inst.tools}
    assert by["druckwerk_get_printers"].annotations.read_only and not by["druckwerk_start_next"].annotations.read_only


async def test_llm_tools_serialise_for_the_mcp_server(hass, hub):
    import probatio

    # The same call mcp_server/server.py _format_tool makes for every tool it lists.
    inst = await llm.async_get_api(hass, API_ID, llm_context())
    for tool in inst.tools:
        schema = probatio.to_openapi(tool.parameters, custom_serializer=inst.custom_serializer, openapi_version="3.1.0")
        assert isinstance(schema["properties"], dict), tool.name
    start = next(t for t in inst.tools if t.name == "druckwerk_start_next")
    schema = probatio.to_openapi(start.parameters, openapi_version="3.1.0")
    assert schema["required"] == ["printer"] and "confirmed_by_person" in schema["properties"]
    assert all("type" in p for p in schema["properties"].values())  # the model needs a type on every parameter


async def test_llm_tool_runs_the_action_as_the_context_user(hass, hub):
    on(hub, "post", f"/api/queue/{HEX}/cancel", dict(JOB, state="cancelled"))
    inst = await llm.async_get_api(hass, API_ID, llm_context("user-ben"))
    res = await inst.async_call_tool(llm.ToolInput("druckwerk_cancel_job", {"job_id": HEX}))
    assert res.data["job"]["state"] == "cancelled" and not res.error
    (post,) = hub_calls(hub, "post", "/cancel")
    assert act_as(post) == "user-ben"
    res = await inst.async_call_tool(llm.ToolInput("druckwerk_get_printers", {}))
    assert res.data["printers"][0]["serial"] == SERIAL
    inst = await llm.async_get_api(hass, API_ID, llm_context(None))
    await inst.async_call_tool(llm.ToolInput("druckwerk_get_queue", {}))
    assert act_as(hub_calls(hub, "get", "/api/queue")[-1]) is None


async def test_llm_start_tool_refused_without_confirmation(hass, hub):
    on(hub, "post", f"/api/printers/{SERIAL}/start-next", JOB)
    inst = await llm.async_get_api(hass, API_ID, llm_context())
    with pytest.raises(ServiceValidationError, match="camera snapshot"):
        await inst.async_call_tool(llm.ToolInput("druckwerk_start_next", {"printer": SERIAL, "plate_clear": True}))
    assert hub_calls(hub, "post", "start-next") == []
    await inst.async_call_tool(llm.ToolInput("druckwerk_start_next", {"printer": SERIAL, "plate_clear": True, "confirmed_by_person": True}))
    assert act_as(hub_calls(hub, "post", "start-next")[0]) == UID


async def test_llm_tool_validates_arguments(hass, hub):
    inst = await llm.async_get_api(hass, API_ID, llm_context())
    with pytest.raises(Exception, match="job_id"):
        await inst.async_call_tool(llm.ToolInput("druckwerk_cancel_job", {}))


async def test_llm_api_is_unregistered_on_unload(hass, hub):
    (entry,) = hass.config_entries.async_entries("druckwerk")
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert API_ID not in [a.id for a in llm.async_get_apis(hass)]
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert API_ID in [a.id for a in llm.async_get_apis(hass)]


async def test_no_hub_no_llm_api_and_clear_action_error(hass, addon, media, vendor_device):
    entry = MockConfigEntry(domain="druckwerk", unique_id="druckwerk", data={"url": "http://local-druckwerk:8098"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert API_ID not in [a.id for a in llm.async_get_apis(hass)]
    with pytest.raises(HomeAssistantError, match="not connected"):
        await act(hass, "get_queue")

