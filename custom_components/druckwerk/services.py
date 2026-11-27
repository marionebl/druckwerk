"""Actions for agents and automations (docs: README "For agents and automations").

Reads answer with SupportsResponse.ONLY, writes with OPTIONAL. Each call passes the calling user's id to
the hub (act-as), so the hub's access rules apply to that user.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv

from .agent import Ops

OPTIONS = vol.Schema(
    {vol.Optional(k): cv.boolean for k in ("timelapse", "bed_leveling", "use_ams")}
)
CONFIRM = {
    vol.Optional("plate_clear", default=False): cv.boolean,
    vol.Optional("confirmed_by_person", default=False): cv.boolean,
}
ONLY, OPTIONAL = SupportsResponse.ONLY, SupportsResponse.OPTIONAL


def _register(hass: HomeAssistant, name: str, schema: Any, response: SupportsResponse, fn) -> None:
    async def handler(call: ServiceCall) -> dict[str, Any]:
        return await fn(Ops(hass), call.context.user_id, call.data)

    hass.services.async_register("druckwerk", name, handler, schema=vol.Schema(schema), supports_response=response)


def async_register_services(hass: HomeAssistant) -> None:
    def job_action(action: str):
        return lambda ops, uid, d: ops.job_action(uid, action, d["job_id"])

    _register(hass, "get_printers", {vol.Optional("printer"): cv.string}, ONLY,
              lambda ops, uid, d: ops.get_printers(uid, d.get("printer")))
    _register(hass, "get_queue", {vol.Optional("history_limit", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=50))},
              ONLY, lambda ops, uid, d: ops.get_queue(uid, d["history_limit"]))
    _register(
        hass, "add_to_queue",
        {
            vol.Optional("file"): cv.string,
            vol.Optional("slice_job_id"): cv.string,
            vol.Optional("target", default="any"): cv.string,
            vol.Optional("model"): cv.string,
            vol.Optional("options"): OPTIONS,
        },
        OPTIONAL,
        lambda ops, uid, d: ops.add_to_queue(
            uid, file=d.get("file"), slice_job_id=d.get("slice_job_id"), target=d["target"],
            model=d.get("model"), options=d.get("options"),
        ),
    )
    _register(
        hass, "slice",
        {
            vol.Required("model_file"): cv.string,
            vol.Required("printer"): cv.string,
            vol.Required("tray"): cv.string,
            vol.Optional("plate"): cv.string,
            vol.Optional("wait", default=True): cv.boolean,
        },
        OPTIONAL,
        lambda ops, uid, d: ops.slice(
            uid, model_file=d["model_file"], printer=d["printer"], tray=d["tray"], plate=d.get("plate"), wait=d["wait"]
        ),
    )
    _register(hass, "get_slice", {vol.Required("slice_job_ids"): vol.All(cv.ensure_list, [cv.string])}, ONLY,
              lambda ops, uid, d: ops.slice_results(uid, d["slice_job_ids"]))
    for action in ("cancel", "skip", "approve", "reject", "retry"):
        _register(hass, f"{action}_job", {vol.Required("job_id"): cv.string}, OPTIONAL, job_action(action))
    _register(
        hass, "reorder_job",
        {vol.Required("job_id"): cv.string, vol.Required("position"): vol.All(vol.Coerce(int), vol.Range(min=0)),
         vol.Optional("printer"): cv.string},
        OPTIONAL,
        lambda ops, uid, d: ops.job_action(uid, "reorder", d["job_id"], position=d["position"], printer=d.get("printer")),
    )
    _register(
        hass, "move_job",
        {vol.Required("job_id"): cv.string, vol.Required("target"): cv.string, vol.Optional("model"): cv.string},
        OPTIONAL,
        lambda ops, uid, d: ops.job_action(uid, "move", d["job_id"], target=d["target"], model=d.get("model")),
    )
    _register(hass, "start_next", {vol.Required("printer"): cv.string, **CONFIRM}, OPTIONAL,
              lambda ops, uid, d: ops.start(uid, d["printer"], None, d["plate_clear"], d["confirmed_by_person"]))
    _register(hass, "start_job", {vol.Required("printer"): cv.string, vol.Required("job_id"): cv.string, **CONFIRM}, OPTIONAL,
              lambda ops, uid, d: ops.start(uid, d["printer"], d["job_id"], d["plate_clear"], d["confirmed_by_person"]))
    _register(hass, "camera_snapshot", {vol.Required("printer"): cv.string}, ONLY,
              lambda ops, uid, d: ops.camera_snapshot(uid, d["printer"]))
