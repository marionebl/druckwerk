"""Print file select: the app lists the files, HA holds the choice."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import callback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import AppEntity
from .platform_helper import add_entities


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    add_entities(entry, async_add_entities, "select", AppSelect)


class AppSelect(AppEntity, SelectEntity, RestoreEntity):
    def __init__(self, hub, serial, desc) -> None:
        super().__init__(hub, serial, desc)
        self._attr_options = self._files

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in self._files:
            self.hub.local[self.serial, self.key] = last.state

    @property
    def _files(self) -> list[str]:
        return list(self.app.get("options", []))

    # Capability attributes are cached by HA, so refresh _attr_options explicitly.
    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_options = self._files
        super()._handle_coordinator_update()

    @property
    def current_option(self) -> str | None:
        # A selection whose file vanished is hidden but remembered if it returns.
        value = self.hub.local.get((self.serial, self.key))
        return value if value in self._files else None

    async def async_select_option(self, option: str) -> None:
        self.hub.local[self.serial, self.key] = option
        self.async_write_ha_state()
