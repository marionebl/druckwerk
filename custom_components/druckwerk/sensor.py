"""Cloud sender result sensor."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .entity import AppEntity
from .platform_helper import add_entities


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    add_entities(entry, async_add_entities, "sensor", AppSensor)


class AppSensor(AppEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, hub, serial, desc) -> None:
        super().__init__(hub, serial, desc)
        self._attr_options = list(desc.get("options", {}))

    @property
    def native_value(self) -> str | None:
        return self.app.get("state")

    @property
    def extra_state_attributes(self) -> dict:
        return self.app.get("attributes", {})
