"""Picture of the plate the printer is printing, from the app."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .entity import AppEntity
from .hub_api import HubError
from .platform_helper import add_entities


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    add_entities(entry, async_add_entities, "image", AppImage)


class AppImage(AppEntity, ImageEntity):
    _attr_content_type = "image/png"

    def __init__(self, hub, serial, desc) -> None:
        AppEntity.__init__(self, hub, serial, desc)
        ImageEntity.__init__(self, hub.hass)
        self._token: str | None = None
        self._png: bytes | None = None
        self._sync()

    def _sync(self) -> None:
        token = self.app.get("token")
        if token != self._token:
            self._token, self._png = token, None
            self._attr_image_last_updated = dt_util.utcnow() if token else None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync()
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict:
        return {"file_path": self.app.get("file_path"), "has_preview": bool(self.app.get("has_preview"))}

    async def async_image(self) -> bytes | None:
        if not self._token:
            return None
        if self._png is None:
            try:
                self._png = await self.hub.call(
                    "GET", f"/api/integration/printers/{self.serial}/image", binary=True
                )
            except HubError as err:
                if err.status != 404:
                    raise
        return self._png
