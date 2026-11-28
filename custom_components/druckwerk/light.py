"""Chamber light through the app (the printer firmware rejects unsigned commands)."""

from __future__ import annotations

from homeassistant.components.light import ColorMode, LightEntity

from .entity import AppEntity
from .platform_helper import add_entities


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    add_entities(entry, async_add_entities, "light", AppLight)


class AppLight(AppEntity, LightEntity):
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, hub, serial, desc) -> None:
        super().__init__(hub, serial, desc)
        self._command = desc["command"].replace("$serial", serial)

    @property
    def is_on(self) -> bool | None:
        return self.app.get("on")

    async def async_turn_on(self, **kwargs) -> None:
        await self.hub.request("POST", self._command, {"on": True}, self.user_id)

    async def async_turn_off(self, **kwargs) -> None:
        await self.hub.request("POST", self._command, {"on": False}, self.user_id)
