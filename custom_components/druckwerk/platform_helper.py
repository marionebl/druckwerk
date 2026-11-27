"""Add entities for printers and descriptors as they show up in the app."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import AppEntity


def add_entities(
    entry: ConfigEntry, async_add_entities: AddEntitiesCallback, platform: str, cls: type[AppEntity]
) -> None:
    """Create entities now and for every printer or descriptor that appears later (no reload)."""
    hub = entry.runtime_data
    known: set[tuple[str, str]] = set()

    def _check() -> None:
        new = [
            (serial, desc)
            for serial in sorted(hub.data or {})
            for desc in hub.manifest.entities
            if desc.get("platform") == platform and (serial, desc["key"]) not in known
        ]
        if new:
            known.update((serial, desc["key"]) for serial, desc in new)
            async_add_entities([cls(hub, serial, desc) for serial, desc in new])

    _check()
    entry.async_on_unload(hub.async_add_listener(_check))
