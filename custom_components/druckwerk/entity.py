"""Shared entity base: one entity per printer and descriptor of the app's manifest."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .externals import VENDOR_DOMAIN, VENDOR_MANUFACTURER
from .hub import HubCoordinator


def _vendor_device(hass, identifiers):
    """The device registered by the vendor integration for these identifiers, if any."""
    for device in dr.async_get(hass).async_get_devices(identifiers=identifiers):
        entry = hass.config_entries.async_get_entry(device.config_entry_id)
        if entry and entry.domain == VENDOR_DOMAIN:
            return device
    return None


class AppEntity(CoordinatorEntity[HubCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, hub: HubCoordinator, serial: str, desc: dict[str, Any]) -> None:
        super().__init__(hub)
        self.hub, self.serial, self.key = hub, serial, desc["key"]
        self._attr_name = desc["name"]
        self._attr_icon = desc.get("icon")
        self._attr_unique_id = f"{serial}_{self.key}"
        identifiers = {(VENDOR_DOMAIN, serial)}
        existing = _vendor_device(hub.hass, identifiers)
        if existing:
            # Since HA 2026.10 a device belongs to one config entry, so identifiers
            # no longer merge across integrations; attach to the existing device itself.
            self.device_entry = existing
        else:
            printer = hub.data.get(serial, {})
            self._attr_device_info = DeviceInfo(
                identifiers=identifiers,
                name=printer.get("name") or serial,
                manufacturer=VENDOR_MANUFACTURER,
                model=printer.get("model"),
                serial_number=serial,
            )

    @property
    def app(self) -> dict[str, Any]:
        """What the app says about this entity."""
        return self.hub.data.get(self.serial, {}).get("entities", {}).get(self.key) or {}

    @property
    def available(self) -> bool:
        # A printer or descriptor the app dropped stays in the registry, unavailable.
        return (
            super().available
            and self.serial in self.hub.data
            and any(d["key"] == self.key for d in self.hub.manifest.entities)
        )
