"""Druckwerk: the Druckwerk app's entities, actions and tools in Home Assistant.

The integration only renders and forwards: an update of it needs a Home Assistant restart,
an update of the app does not. All logic lives in the app (hub/API.md).
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .hub import HubCoordinator
from .upload import UploadView

PLATFORMS = ["select", "switch", "button", "sensor", "camera", "light", "image", "todo"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.http.register_view(UploadView())
    return True


async def _async_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when url or secret changed; recipients go to the app without a reload."""
    hub: HubCoordinator = entry.runtime_data
    if dict(entry.data) != hub.loaded_data:
        await hass.config_entries.async_reload(entry.entry_id)
    elif hub.secret:
        await hub.push_options()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = entry.runtime_data = HubCoordinator(hass, entry)
    entry.async_on_unload(hub.manifest.async_unload)
    if hub.secret:  # without it every call fails
        await hub.manifest.async_load()  # the actions exist before the app answers
        await hub.async_refresh()  # an app that is down must not block the setup; entities appear when it answers
    entry.async_on_unload(entry.add_update_listener(_async_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
