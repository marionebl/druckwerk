"""Local option switches: HA holds the value, the print button sends it."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import AppEntity
from .platform_helper import add_entities


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    add_entities(entry, async_add_entities, "switch", AppSwitch)


class AppSwitch(AppEntity, SwitchEntity, RestoreEntity):
    def __init__(self, hub, serial, desc) -> None:
        super().__init__(hub, serial, desc)
        self._default = bool(desc.get("default"))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        on = last.state == STATE_ON if last and last.state in (STATE_ON, STATE_OFF) else self._default
        self.hub.local[self.serial, self.key] = on

    @property
    def is_on(self) -> bool:
        return self.hub.local.get((self.serial, self.key), self._default)

    async def async_turn_on(self, **kwargs) -> None:
        self.hub.local[self.serial, self.key] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.hub.local[self.serial, self.key] = False
        self.async_write_ha_state()
