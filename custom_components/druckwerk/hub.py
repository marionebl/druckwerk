"""Polls the app's state and forwards calls. The app holds all logic."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import hub_api
from .const import (
    CONF_HUB_SECRET,
    CONF_RECIPIENTS,
    CONF_URL,
    DOMAIN,
    FORWARD_TIMEOUT,
    PRESS_TIMEOUT,
    STATE_INTERVAL,
)
from .manifest import Manifest

_LOGGER = logging.getLogger(__name__)
NETWORK_ERRORS = (aiohttp.ClientError, TimeoutError, ValueError)


class HubCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Every 5 s: GET /api/integration/state. `data` is its `printers`, keyed by serial."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.secret: str = entry.data.get(CONF_HUB_SECRET, "")
        # Without a secret every call fails, so nothing polls.
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=STATE_INTERVAL if self.secret else None
        )
        self.url = hub_api.hub_url_for(entry.data)
        self.bridge_url: str = entry.data[CONF_URL]
        self.loaded_data = dict(entry.data)
        self.local: dict[tuple[str, str], Any] = {}  # (serial, key) -> value of select and switches
        self.manifest = Manifest(hass, entry, self)
        self._push_options = True  # at setup and whenever the app answers again after being down

    async def call(self, method: str, path: str, body: Any = None, **kwargs: Any) -> Any:
        session = async_get_clientsession(self.hass)
        return await hub_api.hub_request(session, self.url, self.secret, method, path, body, **kwargs)

    async def request(
        self, method: str, path: str, body: Any = None, user_id: str | None = None, timeout: float = 60, **kwargs: Any
    ) -> Any:
        """Like call, with errors as HA shows them: 400 is a validation error."""
        try:
            return await self.call(method, path, body, act_as=user_id, timeout=timeout, **kwargs)
        except hub_api.HubError as err:
            if err.status == 400:
                raise ServiceValidationError(err.text) from err
            raise
        except NETWORK_ERRORS as err:
            raise HomeAssistantError(f"Druckwerk not reachable: {err}") from err

    async def forward(self, path: str, body: Any, user_id: str | None) -> dict[str, Any]:
        """POST an action or tool call as the HA user; returns the app's `result`."""
        doc = await self.request("POST", path, body, user_id, timeout=FORWARD_TIMEOUT)
        return doc.get("result") or {}

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        first = self.data is None
        try:
            doc = await self.call("GET", "/api/integration/state")
            revision = doc.get("manifest_revision")
            if first or (revision and revision != self.manifest.revision):
                await self.manifest.async_refresh()
            printers = dict(doc["printers"])
        except (*NETWORK_ERRORS, hub_api.HubError, AttributeError, KeyError) as err:
            self._push_options = True
            raise UpdateFailed(f"Druckwerk not reachable: {err}") from err
        if self._push_options:
            try:
                await self.push_options()
            except (*NETWORK_ERRORS, hub_api.HubError) as err:
                _LOGGER.warning("Cannot push options: %s", err)
        return printers

    async def push_options(self) -> None:
        """Tell the app who gets notifications; the app keeps no other copy."""
        recipients = self.config_entry.options.get(CONF_RECIPIENTS, {})
        await self.call("PUT", "/api/integration/options", {"recipients": recipients})
        self._push_options = False

    async def press(self, serial: str, press: dict[str, Any], user_id: str | None) -> None:
        """Run a button's `press` descriptor (contract: hub/API.md, "How to run press")."""
        for key, text in press.get("needs", {}).items():
            if self.local.get((serial, key)) in (None, ""):
                raise HomeAssistantError(text)
        values = {"$serial": serial, **{f"${k}": v for (s, k), v in self.local.items() if s == serial}}
        body = {k: values.get(v, v) if isinstance(v, str) else v for k, v in press.get("body", {}).items()}
        path = press["path"].replace("$serial", serial)
        as_user = user_id if path.startswith(("/api/actions/", "/api/tools/")) else None
        await self.request("POST", path, body, as_user, timeout=PRESS_TIMEOUT)
        await self.async_request_refresh()
