"""LLM API "Druckwerk": the tools and the prompt come from the app's manifest and run in the app.

Tools run as llm_context.context.user_id, so the app applies that user's rules; no user id
(voice satellite, automation) is the internal admin caller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

try:  # HA 2026.10 validates tool parameters with probatio; voluptuous is only its alias there
    import probatio as vol
except ImportError:  # pragma: no cover
    import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm


if TYPE_CHECKING:
    from .hub import HubCoordinator

API_ID = "druckwerk"
_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool, "object": dict, "array": list}


def _type(spec: dict[str, Any]) -> Any:
    if "enum" in spec:
        return vol.In(spec["enum"])
    if "anyOf" in spec:
        return vol.Any(*(_type(s) for s in spec["anyOf"]))
    if spec.get("type") == "object" and "properties" in spec:
        return schema_from_json(spec)
    if spec.get("type") == "array" and "items" in spec:
        return [_type(spec["items"])]
    return _TYPES.get(spec.get("type"), object)


def schema_from_json(js: dict[str, Any]) -> Any:
    """Small JSON schema (object, properties, required) as a schema the tool API serialises; the app validates the values."""
    required = set(js.get("required", []))
    fields: dict[Any, Any] = {}
    for name, spec in js.get("properties", {}).items():
        kwargs = {"description": spec["description"]} if "description" in spec else {}
        if "default" in spec:
            kwargs["default"] = spec["default"]
        marker = vol.Required if name in required else vol.Optional
        fields[marker(name, **kwargs)] = _type(spec)
    return vol.Schema(fields)


class ProxyTool(llm.Tool):
    """One tool of the manifest; the call goes to the app."""

    integration = "druckwerk"

    def __init__(self, hub: HubCoordinator, spec: dict[str, Any]) -> None:
        self.hub = hub
        self.name = spec["name"]
        self.description = spec.get("description")
        self.parameters = schema_from_json(spec.get("parameters", {}))
        self.annotations = llm.ToolAnnotations(**spec.get("annotations", {}))

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> llm.ToolResult:
        user_id = llm_context.context.user_id if llm_context.context else None
        result = await self.hub.forward(f"/api/tools/{self.name}", tool_input.tool_args, user_id)
        return llm.ToolResult(data=result)


class DruckwerkAPI(llm.API):
    def __init__(self, hass: HomeAssistant, hub: HubCoordinator, llm_doc: dict[str, Any]) -> None:
        super().__init__(hass=hass, id=API_ID, name="Druckwerk")
        self.prompt = llm_doc.get("prompt", "")
        self.tools = [ProxyTool(hub, spec) for spec in llm_doc.get("tools", [])]

    @override
    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        return llm.APIInstance(api=self, api_prompt=self.prompt, llm_context=llm_context, tools=self.tools)
