"""Operations for agents and automations; the HA actions (services.py) and the LLM tools (llm_api.py) share them.

Every call passes the calling HA user's id to the hub as act-as, so the hub applies that user's rules.
No user id (automations, voice satellites) keeps the call internal, which is admin.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any
from urllib.parse import quote

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from . import api
from .const import DOMAIN, MEDIA_SUBDIR, SHARE_ROOT
from .externals import VENDOR_DOMAIN

QUALITIES = ("good", "better", "best")
MODEL_SUFFIXES = (".stl", ".3mf", ".obj", ".step", ".stp")
SLICE_POLL = 5.0  # seconds between slice status checks
SLICE_WAIT = 1800.0  # a call that waits gives up after this; the slices keep running
SNAPSHOT_KEEP = 20  # snapshots kept per printer
JOB_ACTIONS = ("cancel", "retry", "approve", "reject", "skip")
CONFIRM_TEXT = (
    "Starting a print needs plate_clear: true and confirmed_by_person: true. Show the person the sliced plate "
    "review and a current camera snapshot (druckwerk.camera_snapshot), ask them to confirm the plate is clear "
    "and the print may start, and call again only after an explicit yes."
)


def duration(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours} h {rest // 60:02d} min" if hours else f"{rest // 60} min"


def resolve_model(hass: HomeAssistant, path: str) -> str:
    """Real path of a model file inside a media folder or /share (blocking)."""
    if not os.path.isabs(path):
        raise ServiceValidationError("model_file must be an absolute path under /share or /media")
    roots = [os.path.realpath(r) for r in hass.config.media_dirs.values()] + [os.path.realpath(SHARE_ROOT)]
    real = os.path.realpath(path)
    if not any(os.path.commonpath([real, r]) == r for r in roots):
        raise ServiceValidationError("model_file must be inside /share or a media folder")
    if not real.lower().endswith(MODEL_SUFFIXES):
        raise ServiceValidationError("model_file must be one of " + ", ".join(MODEL_SUFFIXES))
    if not os.path.isfile(real):
        raise ServiceValidationError(f"No such file: {path}")
    return real


def serial_from_device(hass: HomeAssistant, device_id: str) -> str | None:
    device = dr.async_get(hass).async_get(device_id)
    if device:
        for domain, serial in device.identifiers:
            if domain == VENDOR_DOMAIN:
                return serial
    return None


def job_summary(job: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "file", "state", "target", "printer", "model", "owner_name", "grams", "print_seconds", "cost",
            "error", "options", "created_at", "started_at", "finished_at")
    out = {k: job[k] for k in keys if k in job}
    if job.get("print_seconds"):
        out["print_time"] = duration(job["print_seconds"])
    return out


class Ops:
    """One instance per HA instance; looks up the hub of the loaded config entry on every call."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def hub(self):
        for entry in self.hass.config_entries.async_loaded_entries(DOMAIN):
            hub = getattr(entry.runtime_data, "hub", None)
            if hub:
                return hub
        raise HomeAssistantError("Druckwerk app is not connected: set the hub secret in the integration options")

    async def call(self, user_id: str | None, method: str, path: str, body: Any = None, **kw: Any) -> Any:
        return await self.hub().call(method, path, body, act_as=user_id, detail=True, **kw)

    async def refresh(self) -> None:
        await self.hub().async_request_refresh()

    # -- printers
    async def serial(self, user_id: str | None, printer: str) -> str:
        """Serial from a serial, a printer name or a device id; unknown text goes to the hub as a serial."""
        printer = printer.strip()
        by_device = serial_from_device(self.hass, printer)
        if by_device:
            return by_device
        for p in await self.call(user_id, "GET", "/api/printers"):
            if printer == p["serial"] or printer.casefold() == str(p.get("name", "")).casefold():
                return p["serial"]
        return printer

    async def get_printers(self, user_id: str | None, printer: str | None = None) -> dict[str, Any]:
        printers = await self.call(user_id, "GET", "/api/printers")
        if printer:
            wanted = await self.serial(user_id, printer)
            printers = [p for p in printers if p["serial"] == wanted]
            if not printers:
                raise ServiceValidationError(f"No printer {printer} visible to you")
        return {"printers": printers}

    # -- queue
    async def get_queue(self, user_id: str | None, history_limit: int = 0) -> dict[str, Any]:
        snap = await self.call(user_id, "GET", "/api/queue")
        waiting: dict[str, list[dict[str, Any]]] = {}
        running, approvals = [], []
        for job in snap["jobs"]:
            state = job.get("state")
            if state in ("starting", "printing"):
                running.append(job_summary(job))
            elif state in ("queued", "pending_approval"):
                key = job.get("target") or "any"
                row = dict(job_summary(job), position=len(waiting.setdefault(key, [])))
                waiting[key].append(row)
                if state == "pending_approval":
                    approvals.append(row)
        return {
            "waiting": waiting,  # per printer serial and "any"; position is 0-based, as reorder_job takes it
            "pending_approvals": approvals,
            "running": running,
            "history": [job_summary(j) for j in snap["history"][: max(0, history_limit)]],
            "printers": snap.get("printers", []),
        }

    async def add_to_queue(
        self,
        user_id: str | None,
        *,
        file: str | None = None,
        slice_job_id: str | None = None,
        target: str = "any",
        model: str | None = None,
        options: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        if bool(file) == bool(slice_job_id):
            raise ServiceValidationError("Give exactly one of file (a sliced file in the share) or slice_job_id")
        if target != "any":
            target = await self.serial(user_id, target)
        body: dict[str, Any] = {"target": target, "options": options or {}}
        if name:
            body["name"] = name
        if slice_job_id:
            if model:
                body["model"] = model
            job = await self.call(user_id, "POST", f"/api/jobs/{slice_job_id}/queue", body)
        else:
            body["file"] = file
            if target == "any":
                body["model"] = model or await self._only_model(user_id)
            job = await self.call(user_id, "POST", "/api/queue", body)
        await self.refresh()
        return {"job": job}

    async def _only_model(self, user_id: str | None) -> str:
        models = {p["model"] for p in await self.call(user_id, "GET", "/api/printers")}
        if len(models) != 1:
            raise ServiceValidationError("target any needs model (the printer model, e.g. H2S) when printers differ")
        return models.pop()

    async def job_action(self, user_id: str | None, action: str, job_id: str, **body: Any) -> dict[str, Any]:
        if action == "reorder" and body.get("printer"):
            body["scope"] = await self.serial(user_id, body.pop("printer"))
        elif action == "move":
            if body.get("target") not in (None, "any"):
                body["target"] = await self.serial(user_id, body["target"])
        body = {k: v for k, v in body.items() if v is not None and k != "printer"}
        job = await self.call(user_id, "POST", f"/api/queue/{job_id}/{action}", body)
        await self.refresh()
        return {"job": job_summary(job) if isinstance(job, dict) else job}

    async def start(
        self, user_id: str | None, printer: str, job_id: str | None, plate_clear: bool, confirmed_by_person: bool
    ) -> dict[str, Any]:
        # Checked here, before any call, so a refused start never reaches the hub.
        if plate_clear is not True or confirmed_by_person is not True:
            raise ServiceValidationError(CONFIRM_TEXT)
        serial = await self.serial(user_id, printer)
        if job_id:
            body = {"job_id": job_id, "plate_clear": True}
            job = await self.call(user_id, "POST", f"/api/printers/{serial}/start", body, timeout=400)
        else:
            job = await self.call(user_id, "POST", f"/api/printers/{serial}/start-next", {"plate_clear": True}, timeout=400)
        await self.refresh()
        return {"job": job_summary(job) if isinstance(job, dict) else job}

    # -- slicing
    async def slice(
        self,
        user_id: str | None,
        *,
        model_file: str,
        printer: str,
        tray: str,
        plate: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Slice good, better and best. The slicer runs one slice at a time, so three variants take minutes."""
        if not re.fullmatch(r"\d+:\d+", tray):
            raise ServiceValidationError("tray is 'unit:slot', both 0-based, as get_printers lists them (e.g. 0:1)")
        real = await self.hass.async_add_executor_job(resolve_model, self.hass, model_file)
        content = await self.hass.async_add_executor_job(_read, real)
        serial = await self.serial(user_id, printer)
        ids: dict[str, str] = {}
        for quality in QUALITIES:
            job = await self.call(
                user_id, "POST", f"/api/jobs?name={quote(os.path.basename(real))}", data=content, timeout=120
            )
            ids[quality] = job["id"]
            body = {"serial": serial, "quality": quality, "tray": tray}
            if plate:
                body["bed"] = plate
            await self.call(user_id, "POST", f"/api/jobs/{job['id']}/slice", body)
        if not wait:
            return {"state": "slicing", "slice_job_ids": ids, "variants": []}
        return await self.slice_results(user_id, list(ids.values()), wait=True, ids=ids)

    async def slice_results(
        self, user_id: str | None, job_ids: list[str], *, wait: bool = False, ids: dict[str, str] | None = None
    ) -> dict[str, Any]:
        deadline = time.monotonic() + SLICE_WAIT
        while True:
            jobs = [await self.call(user_id, "GET", f"/api/jobs/{jid}") for jid in job_ids]
            busy = [j for j in jobs if j.get("state") in ("uploaded", "queued", "slicing")]
            if not (wait and busy):
                break
            if time.monotonic() > deadline:
                raise HomeAssistantError(
                    f"Slicing is still running after {int(SLICE_WAIT)} s; it continues in the app. "
                    f"Ask again with slice_job_ids {', '.join(job_ids)}"
                )
            await asyncio.sleep(SLICE_POLL)
        variants = [await self._variant(user_id, j) for j in jobs]
        done = all(v["state"] == "done" for v in variants)
        failed = any(v["state"] == "failed" for v in variants)
        return {
            "state": "done" if done else "failed" if failed and not busy else "slicing",
            "slice_job_ids": ids or {v["quality"]: v["slice_job_id"] for v in variants},
            "variants": variants,
        }

    async def _variant(self, user_id: str | None, job: dict[str, Any]) -> dict[str, Any]:
        info = job.get("info") or {}
        out: dict[str, Any] = {
            "quality": job.get("quality"),
            "slice_job_id": job["id"],
            "state": job.get("state"),
            "print_seconds": info.get("seconds"),
            "print_time": duration(info.get("seconds")),
            "grams": info.get("grams"),
            "cost": job.get("cost_estimate"),
            "layer_height": ((job.get("settings") or {}).get("process") or "").split(" ")[0] or None,
            "warnings": info.get("warnings") or [],
            "error": job.get("error"),
            "plate_image": None,
        }
        if job.get("state") == "done" and info.get("has_png"):
            out["plate_image"] = await self._save(
                user_id, f"/api/jobs/{job['id']}/plate.png", os.path.join("plates", f"{job['id']}.png")
            )
        return out

    async def _save(self, user_id: str | None, path: str, rel: str) -> str | None:
        """Fetch an image from the hub into the druckwerk media folder; None when that fails."""
        try:
            data = await self.call(user_id, "GET", path, binary=True, timeout=120)
            dest = os.path.join(api.base_dir(self.hass), rel)
            await self.hass.async_add_executor_job(_write, dest, data)
        except (HomeAssistantError, OSError, TimeoutError):
            return None
        return dest

    # -- camera
    async def camera_snapshot(self, user_id: str | None, printer: str) -> dict[str, Any]:
        serial = await self.serial(user_id, printer)
        taken = time.strftime("%Y%m%d-%H%M%S")
        data = await self.call(user_id, "GET", f"/api/printers/{serial}/camera?mode=snapshot", binary=True, timeout=120)
        name = f"{serial}-{taken}.jpg"
        dest = os.path.join(api.base_dir(self.hass), name)
        await self.hass.async_add_executor_job(_write, dest, data)
        await self.hass.async_add_executor_job(_prune, api.base_dir(self.hass), serial)
        return {
            "printer": serial,
            "path": dest,
            "media_content_id": f"media-source://media_source/local/{MEDIA_SUBDIR}/{name}",
            "taken_at": taken,
            "size_bytes": len(data),
        }


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _prune(base: str, serial: str) -> None:
    old = sorted(n for n in os.listdir(base) if n.startswith(serial + "-") and n.endswith(".jpg"))
    for name in old[:-SNAPSHOT_KEEP]:
        os.remove(os.path.join(base, name))
