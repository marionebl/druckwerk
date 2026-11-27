"""The app's manifest: which actions, LLM tools and entities exist. The integration only renders and forwards them.

The last good manifest is cached, so the actions exist at HA start before the app answers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import llm
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.helpers.storage import Store

from .const import API_VERSION, DOMAIN
from .hub_api import HubError
from .devices import serial_from_device
from .llm_api import DruckwerkAPI

if TYPE_CHECKING:
    from .hub import HubCoordinator

_LOGGER = logging.getLogger(__name__)
RESPONSES = {"none": SupportsResponse.NONE, "optional": SupportsResponse.OPTIONAL, "only": SupportsResponse.ONLY}
ISSUE_TOO_NEW = "integration_too_old"
ISSUE_NO_MANIFEST = "app_too_old"


class Manifest:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, hub: HubCoordinator) -> None:
        self.hass = hass
        self.hub = hub
        self.revision: str | None = None
        self.entities: list[dict[str, Any]] = []  # descriptors, one entity per printer each
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.manifest.{entry.entry_id}")
        self._actions: set[str] = set()
        self._unregister_llm = None
        self._cached = False
        self._warned = False

    async def async_load(self) -> None:
        """Register from the cache; the first poll then fetches the live manifest."""
        doc = await self._store.async_load()
        if doc:
            self._cached = True
            self._apply(doc)

    async def async_refresh(self) -> None:
        try:
            doc = await self.hub.call("GET", "/api/integration/manifest")
        except HubError as err:
            if err.status != 404:
                _LOGGER.debug("Manifest not available: %s", err)
                return
            if not self._warned:
                _LOGGER.warning("The Druckwerk app has no manifest; update the app. Using the cached one.")
                self._warned = True
            if not self._cached:
                self._issue(ISSUE_NO_MANIFEST, True)
            return
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Manifest not reachable: %s", err)
            return
        self._issue(ISSUE_NO_MANIFEST, False)
        if doc.get("api_version", 1) > API_VERSION:
            self.revision = doc.get("revision")  # no refetch until the app changes again
            self._issue(ISSUE_TOO_NEW, True)
            return
        self._issue(ISSUE_TOO_NEW, False)
        if doc.get("revision") != self.revision or not self._cached:
            self._apply(doc)
            await self._store.async_save(doc)
            self._cached = True

    def async_unload(self) -> None:
        for name in self._actions:
            self.hass.services.async_remove(DOMAIN, name)
        self._actions.clear()
        if self._unregister_llm:
            self._unregister_llm()
            self._unregister_llm = None

    def _issue(self, issue_id: str, on: bool) -> None:
        if on:
            ir.async_create_issue(
                self.hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key=issue_id
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _apply(self, doc: dict[str, Any]) -> None:
        self.revision = doc.get("revision")
        self.entities = doc.get("entities", [])
        actions = {a["name"]: a for a in doc.get("actions", [])}
        for name in self._actions - actions.keys():
            self.hass.services.async_remove(DOMAIN, name)
        for name, action in actions.items():
            self._register(action)
        self._actions = set(actions)
        if self._unregister_llm:
            self._unregister_llm()
        self._unregister_llm = llm.async_register_api(self.hass, DruckwerkAPI(self.hass, self.hub, doc.get("llm", {})))

    def _register(self, action: dict[str, Any]) -> None:
        name, serial_fields = action["name"], action.get("serial_fields", [])
        response = RESPONSES[action.get("response", "none")]

        async def handler(call: ServiceCall) -> dict[str, Any] | None:
            data = dict(call.data)
            for key in serial_fields:  # the app cannot read the device registry
                if isinstance(data.get(key), str):
                    data[key] = serial_from_device(self.hass, data[key]) or data[key]
            result = await self.hub.forward(f"/api/actions/{name}", data, call.context.user_id)
            return result if response is not SupportsResponse.NONE else None

        self.hass.services.async_register(DOMAIN, name, handler, schema=vol.Schema(dict), supports_response=response)
        async_set_service_schema(
            self.hass,
            DOMAIN,
            name,
            {
                "name": name.replace("_", " ").capitalize(),
                "description": action.get("description", ""),
                "fields": {
                    key: {
                        "name": key.replace("_", " ").capitalize(),
                        **{k: v for k, v in field.items() if k != "required" or v},
                    }
                    for key, field in action.get("fields", {}).items()
                },
            },
        )
