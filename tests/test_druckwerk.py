"""The generic entities: built from the manifest's descriptors, state from the app's state answer."""

import asyncio

import aiohttp
import pytest
from homeassistant.components.camera import CameraEntityFeature, async_get_image, get_camera_from_entity_id
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import mock_restore_cache
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from .conftest import (
    HUB_SECRET, HUB_URL, MANIFEST, SERIAL, STATE, SERIAL2, URL, VENDOR_DOMAIN, VENDOR_MANUFACTURER,
    calls, mock_hub, setup_entry, state_doc, tick, with_entities,
)

SELECT = "select.my_h2s_print_file"
BUTTON = "button.my_h2s_start_cloud_print"
SENSOR = "sensor.my_h2s_cloud_sender"
TIMELAPSE = "switch.my_h2s_print_timelapse"
BED = "switch.my_h2s_print_bed_leveling"
CAMERA = "camera.my_h2s_cloud_camera"
LIGHT = "light.my_h2s_chamber_light_cloud"
IMAGE = "image.my_h2s_current_print"
TODO = "todo.my_h2s_queue"
PRINT = f"{HUB_URL}/api/actions/print_file"
SNAPSHOT = f"{HUB_URL}/api/integration/printers/{SERIAL}/snapshot"
FIRST = STATE["printers"][SERIAL]["entities"]["queue"]["items"][0]["uid"]  # a random id from the real app
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"
EXTRA = {"platform": "switch", "name": "Extra option", "state": "local", "default": True}


def registered(hass, entry):
    return {e.unique_id: e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}


async def press(hass, entity_id=BUTTON, user=None):
    from homeassistant.core import Context
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True, context=Context(user_id=user) if user else Context()
    )


async def choose(hass, option):
    await hass.services.async_call("select", "select_option", {"entity_id": SELECT, "option": option}, blocking=True)


# ---- setup from descriptors
async def test_entities_come_from_the_descriptors_and_attach_to_the_vendor_device(hass, app, vendor_device):
    entry = await setup_entry(hass)
    ents = registered(hass, entry)
    assert set(ents) == {f"{SERIAL}_{d['key']}" for d in MANIFEST["entities"]}
    assert {e.domain for e in ents.values()} == {"select", "switch", "button", "sensor", "camera", "light", "image", "todo"}
    assert {e.device_id for e in ents.values()} == {vendor_device.id}
    devs = list(dr.async_get(hass).devices)
    assert len(devs) == 1 and devs[0].name == "My H2S"  # no second device, no rename
    assert hass.states.get(BUTTON).attributes["friendly_name"] == "My H2S Start cloud print"
    assert er.async_get(hass).async_get("button.my_h2s_re_read_slot_2").original_icon == "mdi:nfc-search-variant"


async def test_without_a_vendor_device_the_integration_registers_one_from_the_state(hass, app):
    await setup_entry(hass)
    dev = next(iter(dr.async_get(hass).async_get_devices(identifiers={(VENDOR_DOMAIN, SERIAL)})), None)
    assert dev and dev.manufacturer == VENDOR_MANUFACTURER and dev.model == "H2S" and dev.name == "Lab"
    assert dev.serial_number == SERIAL


async def test_a_new_descriptor_appears_without_a_reload_and_an_unknown_platform_is_skipped(hass, app, vendor_device):
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    mock_hub(app, manifest=with_entities(extra=EXTRA, odd={"platform": "fan", "name": "Odd"}))
    await tick(hass)
    assert hass.states.get("switch.my_h2s_extra_option").state == "on"  # the descriptor's default
    assert f"{SERIAL}_odd" not in registered(hass, entry)
    assert entry.state is ConfigEntryState.LOADED and entry.runtime_data is runtime


async def test_a_new_printer_appears_without_a_reload_and_the_files_update(hass, app, vendor_device):
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    assert hass.states.get(SELECT).attributes["options"] == ["benchy.gcode.3mf", "sub/cube.gcode.3mf"]
    state = state_doc(SERIAL, SERIAL2, **{SERIAL: {"print_file": {"options": ["new.gcode.3mf"]}}})
    mock_hub(app, state=state)
    await tick(hass)
    assert hass.states.get(SELECT).attributes["options"] == ["new.gcode.3mf"]
    assert {u for u in registered(hass, entry) if u.startswith(SERIAL2)} == {f"{SERIAL2}_{d['key']}" for d in MANIFEST["entities"]}
    assert entry.runtime_data is runtime and entry.state is ConfigEntryState.LOADED


async def test_a_printer_that_vanishes_is_unavailable_and_stays_in_the_registry(hass, app, vendor_device):
    entry = await setup_entry(hass)
    mock_hub(app, state=state_doc(SERIAL2))
    await tick(hass)
    for e in (SELECT, BUTTON, SENSOR, TIMELAPSE, CAMERA, LIGHT, IMAGE, TODO):
        assert hass.states.get(e).state == STATE_UNAVAILABLE, e
    assert f"{SERIAL}_cloud_sender" in registered(hass, entry)


async def test_a_descriptor_that_vanishes_is_unavailable_and_stays_in_the_registry(hass, app, vendor_device):
    entry = await setup_entry(hass)
    mock_hub(app, manifest=with_entities(cloud_sender=None))
    await tick(hass)
    assert hass.states.get(SENSOR).state == STATE_UNAVAILABLE and hass.states.get(SELECT).state != STATE_UNAVAILABLE
    assert f"{SERIAL}_cloud_sender" in registered(hass, entry)


# ---- the app is down
async def test_app_down_makes_entities_unavailable_until_it_answers(hass, app, vendor_device):
    await setup_entry(hass)
    assert hass.states.get(SELECT).state != STATE_UNAVAILABLE
    app.get(f"{HUB_URL}/api/integration/state", exc=aiohttp.ClientError)
    app._mocks.insert(0, app._mocks.pop())
    await tick(hass)
    for e in (SELECT, BUTTON, SENSOR, TIMELAPSE, BED, CAMERA, LIGHT, IMAGE, TODO):
        assert hass.states.get(e).state == STATE_UNAVAILABLE, e
    mock_hub(app)
    await tick(hass, 12)
    assert hass.states.get(SENSOR).state == "sent"


async def test_a_401_from_the_app_is_a_failed_poll(hass, app, vendor_device):
    await setup_entry(hass)
    app.get(f"{HUB_URL}/api/integration/state", status=401, json={"error": "no"})
    app._mocks.insert(0, app._mocks.pop())
    await tick(hass)
    assert hass.states.get(SENSOR).state == STATE_UNAVAILABLE


async def test_setup_does_not_wait_for_an_app_that_is_down_and_entities_appear_later(hass, app, vendor_device):
    app._mocks = []
    app.get(f"{HUB_URL}/api/integration/state", exc=aiohttp.ClientError)
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED and not registered(hass, entry)
    mock_hub(app)
    await tick(hass)
    assert hass.states.get(SENSOR) is not None and f"{SERIAL}_print_file" in registered(hass, entry)


# ---- select and switches: HA holds the state
async def test_restore_selection_and_switches(hass, app, vendor_device):
    entry = await setup_entry(hass)  # registers the entity ids
    await hass.config_entries.async_unload(entry.entry_id)
    mock_restore_cache(hass, [State(SELECT, "sub/cube.gcode.3mf"), State(TIMELAPSE, "on"), State(BED, "off")])
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(SELECT).state == "sub/cube.gcode.3mf"
    assert hass.states.get(TIMELAPSE).state == "on" and hass.states.get(BED).state == "off"


async def test_switches_start_with_the_descriptor_default_and_a_selection_outside_the_options_is_not_restored(hass, app, vendor_device):
    entry = await setup_entry(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    mock_restore_cache(hass, [State(SELECT, "gone.gcode.3mf")])
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(SELECT).state == "unknown"
    assert hass.states.get(TIMELAPSE).state == "off" and hass.states.get(BED).state == "on"


async def test_a_selected_file_that_vanishes_is_remembered_when_it_returns(hass, app, vendor_device):
    await setup_entry(hass)
    await choose(hass, "sub/cube.gcode.3mf")
    assert hass.states.get(SELECT).state == "sub/cube.gcode.3mf"
    mock_hub(app, state=state_doc(**{SERIAL: {"print_file": {"options": ["benchy.gcode.3mf"]}}}))
    await tick(hass)
    assert hass.states.get(SELECT).state == "unknown"
    mock_hub(app)
    await tick(hass, 12)
    assert hass.states.get(SELECT).state == "sub/cube.gcode.3mf"


async def test_switches_toggle_in_ha_only(hass, app, vendor_device):
    await setup_entry(hass)
    await hass.services.async_call("switch", "turn_on", {"entity_id": TIMELAPSE}, blocking=True)
    await hass.services.async_call("switch", "turn_off", {"entity_id": BED}, blocking=True)
    assert hass.states.get(TIMELAPSE).state == "on" and hass.states.get(BED).state == "off"
    assert not [c for c in app.mock_calls if c[0] != "GET" and not str(c[1]).endswith("/options")]


# ---- buttons: run the descriptor's press
async def test_start_print_sends_the_selected_file_and_the_switch_values_as_the_user(hass, app, vendor_device):
    app.post(PRINT, json={"result": {"result": "sent"}})
    await setup_entry(hass)
    await choose(hass, "sub/cube.gcode.3mf")
    await hass.services.async_call("switch", "turn_on", {"entity_id": TIMELAPSE}, blocking=True)
    await hass.services.async_call("switch", "turn_off", {"entity_id": BED}, blocking=True)
    anna = await hass.auth.async_create_user("Anna")
    await press(hass, user=anna.id)
    (post,) = calls(app, "POST", "/api/actions/print_file")
    assert post[2] == {"serial": SERIAL, "file": "sub/cube.gcode.3mf", "timelapse": True, "bed_leveling": False, "use_ams": True}
    assert post[3]["X-Hub-Secret"] == HUB_SECRET and post[3]["X-Druckwerk-Act-As"] == anna.id


async def test_start_print_without_a_selection_calls_nothing(hass, app, vendor_device):
    await setup_entry(hass)
    with pytest.raises(HomeAssistantError, match="Select a print file first"):
        await press(hass)
    assert not calls(app, "POST", "/print_file")


async def test_start_print_shows_the_apps_error_text(hass, app, vendor_device):
    await setup_entry(hass)
    await choose(hass, "benchy.gcode.3mf")
    app.post(PRINT, status=409, json={"error": "Printer X is RUNNING; refusing to send a new print"})
    with pytest.raises(HomeAssistantError, match="is RUNNING") as err:
        await press(hass)
    assert not isinstance(err.value, ServiceValidationError)
    app._mocks.clear()
    mock_hub(app)
    app.post(PRINT, status=400, json={"error": "File not found: benchy.gcode.3mf"})
    with pytest.raises(ServiceValidationError, match="File not found"):
        await press(hass)


async def test_a_press_polls_the_state_at_once(hass, app, vendor_device):
    app.post(PRINT, json={"result": {}})
    await setup_entry(hass)
    await choose(hass, "benchy.gcode.3mf")
    before = len(calls(app, "GET", "/api/integration/state"))
    await press(hass)
    await hass.async_block_till_done()
    assert len(calls(app, "GET", "/api/integration/state")) == before + 1


async def test_reread_slot_posts_to_the_rfid_route_without_act_as(hass, app, vendor_device):
    app.post(f"{HUB_URL}/api/integration/printers/{SERIAL}/rfid", json={"sent": 1})
    await setup_entry(hass)
    anna = await hass.auth.async_create_user("Anna")
    await press(hass, "button.my_h2s_re_read_slot_3", user=anna.id)
    (post,) = calls(app, "POST", f"/printers/{SERIAL}/rfid")
    assert post[2] == {"ams_id": 0, "slot_id": 2} and "X-Druckwerk-Act-As" not in post[3]


async def test_reread_error_surfaces(hass, app, vendor_device):
    app.post(f"{HUB_URL}/api/integration/printers/{SERIAL}/rfid", status=502, json={"error": "send_message returned -2"})
    await setup_entry(hass)
    with pytest.raises(HomeAssistantError, match="send_message returned -2"):
        await press(hass, "button.my_h2s_re_read_slot_1")


async def test_an_unreachable_app_gives_a_clear_press_error(hass, app, vendor_device):
    app.post(f"{HUB_URL}/api/integration/printers/{SERIAL}/rfid", exc=aiohttp.ClientError("down"))
    await setup_entry(hass)
    with pytest.raises(HomeAssistantError, match="not reachable"):
        await press(hass, "button.my_h2s_re_read_slot_1")


# ---- sensor
async def test_sensor_shows_the_apps_state_and_attributes(hass, app, vendor_device):
    await setup_entry(hass)
    st = hass.states.get(SENSOR)
    assert st.state == "sent" and st.attributes["options"] == ["idle", "sent", "error"]
    assert st.attributes["last_file"] == "benchy.gcode.3mf" and st.attributes["account_email"] == "a@example.invalid"
    assert st.attributes["last_error"] is None


async def test_sensor_state_labels_are_translated(hass, app, vendor_device):
    import json
    from pathlib import Path

    from homeassistant.helpers import entity_registry as er

    await setup_entry(hass)
    assert er.async_get(hass).async_get(SENSOR).translation_key == "cloud_sender"
    for name in ("strings.json", "translations/en.json"):
        text = json.loads((Path(__file__).parents[1] / "custom_components/druckwerk" / name).read_text())
        assert text["entity"]["sensor"]["cloud_sender"]["state"] == {"idle": "Idle", "sent": "Sent", "error": "Error"}


# ---- camera
async def test_camera_streams_from_the_bridge_url(hass, app, vendor_device):
    await setup_entry(hass)
    cam = get_camera_from_entity_id(hass, CAMERA)
    assert cam.supported_features & CameraEntityFeature.STREAM
    assert await cam.stream_source() == f"{URL}/api/camera/{SERIAL}/live"  # the bridge, not the app
    assert hass.states.get(CAMERA).attributes["online"] is True


async def test_a_jpeg_printer_stream_is_encoded_to_h264(hass, app, vendor_device):
    state = state_doc()
    state["printers"][SERIAL]["entities"]["cloud_camera"]["transcode"] = True
    mock_hub(app, state=state)
    await setup_entry(hass)
    cam = get_camera_from_entity_id(hass, CAMERA)
    assert await cam.stream_source() == f"ffmpeg:{URL}/api/camera/{SERIAL}/live#video=h264"


async def test_an_offline_camera_has_no_stream_and_shows_the_apps_placeholder(hass, app, vendor_device):
    offline = {"cloud_camera": {"online": False, "stream": None, "transcode": False,
                                "snapshot": f"/api/integration/printers/{SERIAL}/snapshot"}}
    mock_hub(app, state=state_doc(**{SERIAL: offline}))
    app.get(SNAPSHOT, content=JPEG + b"offline", headers={"Content-Type": "image/jpeg"})
    await setup_entry(hass)
    cam = get_camera_from_entity_id(hass, CAMERA)
    assert not cam.supported_features & CameraEntityFeature.STREAM
    assert await cam.stream_source() is None
    assert hass.states.get(CAMERA).attributes["online"] is False
    assert (await async_get_image(hass, CAMERA)).content == JPEG + b"offline"


async def test_camera_serves_the_snapshot_with_the_secret_and_caches_it(hass, app, vendor_device):
    app.get(SNAPSHOT, content=JPEG, headers={"Content-Type": "image/jpeg"})
    await setup_entry(hass)
    assert (await async_get_image(hass, CAMERA)).content == JPEG
    await async_get_image(hass, CAMERA)
    snaps = calls(app, "GET", "/snapshot")
    assert len(snaps) == 1 and snaps[0][3]["X-Hub-Secret"] == HUB_SECRET  # the second view uses the cache
    assert hass.states.get(CAMERA).attributes["frame_time"]


async def test_camera_error_is_reported_and_the_last_image_is_kept(hass, app, vendor_device):
    app.get(SNAPSHOT, status=502, json={"error": "timeout after 20s"})
    await setup_entry(hass)
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, CAMERA)
    await hass.async_block_till_done()
    assert hass.states.get(CAMERA).attributes["last_error"].startswith("HTTP 502: ")
    assert "timeout after 20s" in hass.states.get(CAMERA).attributes["last_error"]


async def test_camera_does_not_serve_a_stale_frame(hass, app, vendor_device):
    app.get(SNAPSHOT, content=JPEG, headers={"Content-Type": "image/jpeg"})
    await setup_entry(hass)
    await async_get_image(hass, CAMERA)
    get_camera_from_entity_id(hass, CAMERA)._fetched -= 120  # age the frame; patching time would freeze the loop
    app._mocks = [m for m in app._mocks if not str(m.url).endswith("/snapshot")]

    async def slow(method, url, data):  # a real request suspends; HA starts the fetch task eagerly
        await asyncio.sleep(0.01)
        return AiohttpClientMockResponse(method, url, response=JPEG + b"new", headers={"Content-Type": "image/jpeg"})

    app.get(SNAPSHOT, side_effect=slow)
    assert (await async_get_image(hass, CAMERA)).content == JPEG + b"new"  # waited for the new frame


# ---- light
async def test_light_shows_the_apps_state_and_commands_go_through_the_app(hass, app, vendor_device):
    app.post(f"{HUB_URL}/api/integration/printers/{SERIAL}/light", json={"sent": 2})
    await setup_entry(hass)
    assert hass.states.get(LIGHT).state == "on"
    await hass.services.async_call("light", "turn_off", {"entity_id": LIGHT}, blocking=True)
    await hass.services.async_call("light", "turn_on", {"entity_id": LIGHT}, blocking=True)
    posts = calls(app, "POST", f"/printers/{SERIAL}/light")
    assert [p[2] for p in posts] == [{"on": False}, {"on": True}]
    assert posts[0][3]["X-Hub-Secret"] == HUB_SECRET
    mock_hub(app, state=state_doc(**{SERIAL: {"chamber_light": {"on": None}}}))
    await tick(hass)
    assert hass.states.get(LIGHT).state == "unknown"


async def test_a_light_command_error_shows_the_apps_text(hass, app, vendor_device):
    app.post(f"{HUB_URL}/api/integration/printers/{SERIAL}/light", status=404, json={"error": "unknown printer"})
    await setup_entry(hass)
    with pytest.raises(HomeAssistantError, match="unknown printer"):
        await hass.services.async_call("light", "turn_on", {"entity_id": LIGHT}, blocking=True)


# ---- image
async def test_image_follows_the_token_and_fetches_the_picture_from_the_app(hass, app, vendor_device):
    png = b"\x89PNG\r\n\x1a\nfake"
    app.get(f"{HUB_URL}/api/integration/printers/{SERIAL}/image", content=png)
    await setup_entry(hass)
    ent = hass.data["entity_components"]["image"].get_entity(IMAGE)
    first = ent.image_last_updated
    assert first and await ent.async_image() == png and await ent.async_image() == png
    assert len(calls(app, "GET", "/image")) == 1  # cached until the token changes
    assert calls(app, "GET", "/image")[0][3]["X-Hub-Secret"] == HUB_SECRET
    attrs = hass.states.get(IMAGE).attributes
    assert attrs["file_path"] == "/media/druckwerk/benchy.gcode.3mf" and attrs["has_preview"] is True
    mock_hub(app)
    await tick(hass)
    assert ent.image_last_updated == first  # same token, same picture
    token = {"current_print": {"token": "aaaaaaaaaaaa", "file_path": "/media/druckwerk/b.gcode.3mf", "has_preview": True}}
    mock_hub(app, state=state_doc(**{SERIAL: token}))
    await tick(hass)
    assert ent.image_last_updated > first and ent._png is None


async def test_image_without_a_token_has_no_picture_and_a_404_is_no_picture(hass, app, vendor_device):
    none = {"current_print": {"token": None, "file_path": None, "has_preview": False}}
    mock_hub(app, state=state_doc(**{SERIAL: none}))
    await setup_entry(hass)
    ent = hass.data["entity_components"]["image"].get_entity(IMAGE)
    assert ent.image_last_updated is None and await ent.async_image() is None
    assert not calls(app, "GET", "/image")
    app.get(f"{HUB_URL}/api/integration/printers/{SERIAL}/image", status=404, json={"error": "no preview"})
    mock_hub(app)
    app.get(f"{HUB_URL}/api/integration/printers/{SERIAL}/image", status=404, json={"error": "no preview"})
    await tick(hass)
    assert await ent.async_image() is None


# ---- todo
async def test_todo_lists_the_apps_items_in_order(hass, app, vendor_device):
    await setup_entry(hass)
    r = await hass.services.async_call("todo", "get_items", {"entity_id": TODO}, blocking=True, return_response=True)
    first, second = r[TODO]["items"]
    assert (first["summary"], first["description"]) == ("First", "File: a.gcode.3mf\nOwner: U-ADMIN")
    assert second["description"].endswith("(needs approval)") and second["status"] == "needs_action"
    assert not hass.states.get(TODO).attributes["supported_features"] & 1  # no create


async def test_todo_delete_cancels_each_job_as_the_integration(hass, app, vendor_device):
    app.post(f"{HUB_URL}/api/queue/{FIRST}/cancel", json={})
    await setup_entry(hass)
    await hass.services.async_call("todo", "remove_item", {"entity_id": TODO, "item": FIRST}, blocking=True)
    (post,) = calls(app, "POST", f"/api/queue/{FIRST}/cancel")
    assert post[2] == {} and post[3]["X-Hub-Secret"] == HUB_SECRET and "X-Druckwerk-Act-As" not in post[3]


async def test_todo_move_computes_the_position_among_the_waiting_jobs(hass, app, vendor_device):
    items = [{"uid": u, "summary": u, "description": ""} for u in ("bbbbbbbbbbbb", "cccccccccccc", "aaaaaaaaaaaa")]
    mock_hub(app, state=state_doc(**{SERIAL: {"queue": {"items": items}}}))
    app.post(f"{HUB_URL}/api/queue/aaaaaaaaaaaa/reorder", json={})
    await setup_entry(hass)
    ent = hass.data["entity_components"]["todo"].get_entity(TODO)
    await ent.async_move_todo_item("aaaaaaaaaaaa", "bbbbbbbbbbbb")
    await ent.async_move_todo_item("aaaaaaaaaaaa", None)
    await ent.async_move_todo_item("aaaaaaaaaaaa", "gone")
    assert [c[2] for c in calls(app, "POST", "/reorder")] == [
        {"position": 1, "scope": SERIAL}, {"position": 0, "scope": SERIAL}, {"position": 2, "scope": SERIAL}]


async def test_a_todo_error_is_prefixed(hass, app, vendor_device):
    app.post(f"{HUB_URL}/api/queue/{FIRST}/cancel", status=403, json={"error": "rule: not yours"})
    await setup_entry(hass)
    with pytest.raises(HomeAssistantError, match="Druckwerk: rule: not yours"):
        await hass.services.async_call("todo", "remove_item", {"entity_id": TODO, "item": FIRST}, blocking=True)
