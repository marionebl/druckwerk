"""Buttons: pressing runs the descriptor's `press` in the app."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity

from .entity import AppEntity
from .platform_helper import add_entities


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    add_entities(entry, async_add_entities, "button", AppButton)


class AppButton(AppEntity, ButtonEntity):
    def __init__(self, hub, serial, desc) -> None:
        super().__init__(hub, serial, desc)
        self._press = desc["press"]

    async def async_press(self) -> None:
        await self.hub.press(self.serial, self._press, self.user_id)
