"""Setup does no network call; the first poll waits until Home Assistant has started."""

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState

from .conftest import URL, HUB_SECRET, mock_hub
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_no_network_call_before_home_assistant_has_started(hass, aioclient_mock):
    mock_hub(aioclient_mock)
    hass.set_state(CoreState.starting)
    entry = MockConfigEntry(domain="druckwerk", unique_id="druckwerk", data={"url": URL, "hub_secret": HUB_SECRET})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert aioclient_mock.call_count == 0

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert aioclient_mock.call_count > 0
    assert entry.runtime_data.data
