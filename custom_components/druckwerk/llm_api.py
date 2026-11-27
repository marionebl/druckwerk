"""LLM API "Druckwerk": the same operations as the actions, for HA's MCP server and Assist.

Registered with llm.async_register_api (HA's mcp_server lists llm.async_get_apis; Assist and
conversation agents pick it by id). Tools run as llm_context.context.user_id, so the hub applies
that user's rules; no user id (voice satellite, automation) is the internal admin caller.
"""

from __future__ import annotations

from typing import Any, override

try:  # HA 2026.10 validates tool parameters with probatio; voluptuous is only its alias there
    import probatio as vol
except ImportError:  # pragma: no cover
    import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from .agent import CONFIRM_TEXT, Ops

API_ID = "druckwerk"
PROMPT = """Druckwerk manages 3D printers: printer status, the print queue, slicing and starting prints.
Rules:
- You act as the signed-in Home Assistant user. Home Assistant admins and printer owners are admins of their printers.
  Users may add jobs on printers they have an active grant for (some grants need an admin's approval), cancel their own jobs
  and reorder only within their own queue slots. Only admins skip, move, approve, reject, retry and start jobs. A refusal
  starting with "rule:" is final; tell the person, do not try another way round.
- Slicing takes minutes (one slice at a time, three variants good, better and best). Say so before you start, and use
  wait false with druckwerk_get_slice_results when your own call timeout is short.
- Typical flow: druckwerk_get_printers (serial, AMS trays as unit:slot), druckwerk_slice, let the person choose a variant,
  druckwerk_add_to_queue with its slice_job_id. Adding to the queue never starts a print.
- Never start a print on your own. Before druckwerk_start_next or druckwerk_start_job, show the person the sliced plate
  review and a current camera snapshot (druckwerk_camera_snapshot), ask whether the plate is clear and the print may
  start, and call only after an explicit yes, with plate_clear and confirmed_by_person both true. Without both the call is refused.
- Printers are named by serial number, printer name or device id. Job ids are 12 hex characters.
"""

# Plain types, not cv.string/cv.boolean: to_openapi gives those no "type", and the model needs it.
JOB_ID = {vol.Required("job_id", description="Queue job id from druckwerk_get_queue"): str}
PRINTER = {vol.Required("printer", description="Printer serial number or name"): str}
READ = llm.ToolAnnotations(read_only=True, destructive=False, idempotent=True, open_world=False)
WRITE = llm.ToolAnnotations(read_only=False, destructive=False, idempotent=False, open_world=False)
CANCEL = llm.ToolAnnotations(read_only=False, destructive=True, idempotent=False, open_world=False)
PHYSICAL = llm.ToolAnnotations(read_only=False, destructive=True, idempotent=False, open_world=True)


class DruckwerkTool(llm.Tool):
    """Base: validates the arguments and runs one operation as the calling user."""

    integration = "druckwerk"
    annotations = WRITE

    async def run(self, ops: Ops, user_id: str | None, args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> llm.ToolResult:
        args = self.parameters(tool_input.tool_args)  # the API instance does not validate
        user_id = llm_context.context.user_id if llm_context.context else None
        return llm.ToolResult(data=await self.run(Ops(hass), user_id, args))


class GetPrinters(DruckwerkTool):
    name = "druckwerk_get_printers"
    description = (
        "Status of the printers you may use: state, progress, remaining time, end time, layer, temperatures, "
        "AMS trays (unit and slot), HMS errors, current job, queue length."
    )
    parameters = vol.Schema({vol.Optional("printer", description="Only this printer: serial or name"): str})
    annotations = READ

    async def run(self, ops, user_id, args):
        return await ops.get_printers(user_id, args.get("printer"))


class GetQueue(DruckwerkTool):
    name = "druckwerk_get_queue"
    description = (
        "The print queue: waiting jobs per printer serial and for any (with 0-based position), pending approvals, "
        "running jobs, optional history."
    )
    parameters = vol.Schema(
        {vol.Optional("history_limit", default=0, description="Finished jobs to include, 0 to 50"): vol.All(int, vol.Range(min=0, max=50))}
    )
    annotations = READ

    async def run(self, ops, user_id, args):
        return await ops.get_queue(user_id, args["history_limit"])


class AddToQueue(DruckwerkTool):
    name = "druckwerk_add_to_queue"
    description = (
        "Add a job to the queue from a sliced file name in the share or from a slice_job_id (give one). "
        "Does not start a print."
    )
    parameters = vol.Schema(
        {
            vol.Optional("file", description="Sliced .gcode.3mf name in the share"): str,
            vol.Optional("slice_job_id", description="Slice job id from druckwerk_slice"): str,
            vol.Optional("target", default="any", description="Printer serial or name, or any"): str,
            vol.Optional("model", description="Printer model for target any, e.g. H2S"): str,
            vol.Optional("options", description="timelapse, bed_leveling, use_ams as booleans"): vol.Schema(
                {vol.Optional(k): bool for k in ("timelapse", "bed_leveling", "use_ams")}
            ),
        }
    )

    async def run(self, ops, user_id, args):
        return await ops.add_to_queue(
            user_id, file=args.get("file"), slice_job_id=args.get("slice_job_id"), target=args["target"],
            model=args.get("model"), options=args.get("options"),
        )


class Slice(DruckwerkTool):
    name = "druckwerk_slice"
    description = (
        "Slice a model (.stl, .3mf, .obj, .step under /share or /media) as good, better and best for one printer and "
        "tray; returns time, grams, cost and a slice job id per variant. Takes 3 to 15 minutes; wait false returns "
        "the ids at once."
    )
    parameters = vol.Schema(
        {
            vol.Required("model_file", description="Absolute path of the model"): str,
            vol.Required("printer", description="Printer serial or name"): str,
            vol.Required("tray", description="AMS tray as unit:slot, 0-based, e.g. 0:0"): str,
            vol.Optional("plate", description="Build plate type, default is the printer's"): str,
            vol.Optional("wait", default=True, description="Wait for the results"): bool,
        }
    )

    async def run(self, ops, user_id, args):
        return await ops.slice(
            user_id, model_file=args["model_file"], printer=args["printer"], tray=args["tray"],
            plate=args.get("plate"), wait=args["wait"],
        )


class GetSliceResults(DruckwerkTool):
    name = "druckwerk_get_slice_results"
    description = "State, print time, grams and cost of slice jobs (after druckwerk_slice with wait false)."
    parameters = vol.Schema({vol.Required("slice_job_ids", description="Slice job ids"): [str]})
    annotations = READ

    async def run(self, ops, user_id, args):
        return await ops.slice_results(user_id, args["slice_job_ids"])


def _job_tool(action: str, description: str, annotations: llm.ToolAnnotations = WRITE) -> type[DruckwerkTool]:
    class JobTool(DruckwerkTool):
        name = f"druckwerk_{action}_job"
        parameters = vol.Schema(JOB_ID)

        async def run(self, ops, user_id, args):
            return await ops.job_action(user_id, action, args["job_id"])

    JobTool.description = description
    JobTool.annotations = annotations
    JobTool.__name__ = JobTool.__qualname__ = f"{action.title()}Job"
    return JobTool


CancelJob = _job_tool("cancel", "Cancel a waiting job. Users cancel their own, admins any.", CANCEL)
SkipJob = _job_tool("skip", "Send a waiting job to the end of the queue. Admins only.")
ApproveJob = _job_tool("approve", "Approve a job waiting for approval. Admins only.")
RejectJob = _job_tool("reject", "Reject a job waiting for approval. Admins only.", CANCEL)
RetryJob = _job_tool("retry", "Put a failed or cancelled job back into the queue. Admins only.")


class ReorderJob(DruckwerkTool):
    name = "druckwerk_reorder_job"
    description = "Move a waiting job to a 0-based position. Users reorder only within their own slots, admins any."
    parameters = vol.Schema(
        {
            **JOB_ID,
            vol.Required("position", description="New 0-based position"): vol.All(int, vol.Range(min=0)),
            vol.Optional("printer", description="Count positions among this printer's waiting jobs"): str,
        }
    )

    async def run(self, ops, user_id, args):
        return await ops.job_action(user_id, "reorder", args["job_id"], position=args["position"], printer=args.get("printer"))


class MoveJob(DruckwerkTool):
    name = "druckwerk_move_job"
    description = "Change a waiting job's target printer, or to any. Admins only."
    parameters = vol.Schema(
        {
            **JOB_ID,
            vol.Required("target", description="Printer serial or name, or any"): str,
            vol.Optional("model", description="Printer model, needed for any"): str,
        }
    )

    async def run(self, ops, user_id, args):
        return await ops.job_action(user_id, "move", args["job_id"], target=args["target"], model=args.get("model"))


_CONFIRM = {
    vol.Optional("plate_clear", default=False, description="True only after the person saw a current camera snapshot"): bool,
    vol.Optional("confirmed_by_person", default=False, description="True only after the person explicitly said to start"): bool,
}
_SAFETY = " Admins only. " + CONFIRM_TEXT


class StartNext(DruckwerkTool):
    name = "druckwerk_start_next"
    description = "Start the next queued job on a printer." + _SAFETY
    parameters = vol.Schema({**PRINTER, **_CONFIRM})
    annotations = PHYSICAL

    async def run(self, ops, user_id, args):
        return await ops.start(user_id, args["printer"], None, args["plate_clear"], args["confirmed_by_person"])


class StartJob(DruckwerkTool):
    name = "druckwerk_start_job"
    description = "Start a chosen queued job on a printer, ahead of the queue order." + _SAFETY
    parameters = vol.Schema({**PRINTER, **JOB_ID, **_CONFIRM})
    annotations = PHYSICAL

    async def run(self, ops, user_id, args):
        return await ops.start(user_id, args["printer"], args["job_id"], args["plate_clear"], args["confirmed_by_person"])


class CameraSnapshot(DruckwerkTool):
    name = "druckwerk_camera_snapshot"
    description = "Take a current camera picture; saved under /media/druckwerk, returns its path. Takes up to a minute."
    parameters = vol.Schema(PRINTER)
    annotations = llm.ToolAnnotations(read_only=True, destructive=False, idempotent=False, open_world=False)

    async def run(self, ops, user_id, args):
        return await ops.camera_snapshot(user_id, args["printer"])


TOOLS: tuple[type[DruckwerkTool], ...] = (
    GetPrinters, GetQueue, AddToQueue, Slice, GetSliceResults, CancelJob, ReorderJob, MoveJob, SkipJob,
    ApproveJob, RejectJob, RetryJob, StartNext, StartJob, CameraSnapshot,
)


class DruckwerkAPI(llm.API):
    """Fixed tool set; which calls succeed is decided by the hub per user."""

    @override
    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        return llm.APIInstance(
            api=self, api_prompt=PROMPT, llm_context=llm_context, tools=[tool() for tool in TOOLS]
        )


def async_register(hass: HomeAssistant):
    """Register the API; returns the unregister callback."""
    if any(api.id == API_ID for api in llm.async_get_apis(hass)):
        raise HomeAssistantError("Druckwerk LLM API is already registered")
    return llm.async_register_api(hass, DruckwerkAPI(hass=hass, id=API_ID, name="Druckwerk"))
