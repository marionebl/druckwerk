"""Camera through the app: a live stream (go2rtc plays it) and stills through the snapshot route."""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import CAMERA_MAX_AGE, CAMERA_STALE, CAMERA_TIMEOUT, HUB_SECRET_HEADER
from .entity import AppEntity
from .platform_helper import add_entities

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    add_entities(entry, async_add_entities, "camera", AppCamera)


class AppCamera(AppEntity, Camera):
    # Each snapshot may open a new cloud tunnel and wait for a keyframe (10-60 s under qemu).
    # So the entity serves the last image and refreshes it in the background.

    _unrecorded_attributes = frozenset({"frame_time"})

    def __init__(self, hub, serial, desc) -> None:
        AppEntity.__init__(self, hub, serial, desc)
        Camera.__init__(self)
        self._image: bytes | None = None
        self._fetched = 0.0
        self._task: asyncio.Task | None = None
        self._frame_time: str | None = None
        self._last_error: str | None = None
        self._written: tuple[bool, str | None] | None = None  # (online, last_error) of the last state write

    @property
    def online(self) -> bool:
        return bool(self.app.get("online"))

    @property
    def supported_features(self) -> CameraEntityFeature:
        # Offline, the frontend shows the still image instead of a stream that can only fail.
        return CameraEntityFeature.STREAM if self.online else CameraEntityFeature(0)

    @property
    def extra_state_attributes(self) -> dict:
        return {"online": self.online, "frame_time": self._frame_time, "last_error": self._last_error}

    async def _fetch(self) -> None:
        # The app answers a placeholder picture while the printer is offline.
        session = async_get_clientsession(self.hass)
        url = self.hub.url + self.app.get("snapshot", "")
        try:
            async with session.get(
                url, headers={HUB_SECRET_HEADER: self.hub.secret}, timeout=aiohttp.ClientTimeout(total=CAMERA_TIMEOUT)
            ) as resp:
                body = await resp.read()
                if resp.status == 200 and resp.content_type == "image/jpeg":
                    self._image, error = body, None
                    self._frame_time = dt_util.utcnow().isoformat()
                else:
                    error = f"HTTP {resp.status}: {body[:300].decode(errors='replace')}"
        except (aiohttp.ClientError, TimeoutError) as err:
            error = f"{type(err).__name__}: {err}"
        self._fetched = time.monotonic()
        if error:
            _LOGGER.debug("Snapshot for %s failed: %s", self.serial, error)
        self._last_error = error
        # frame_time changes with each still; writing for it would fill the database. The first still is written.
        if self._written != (self.online, error):
            self._written = (self.online, error)
            self.async_write_ha_state()

    async def stream_source(self) -> str | None:
        stream = self.app.get("stream")
        if not (self.online and stream):
            return None
        # The app shares one cloud tunnel between all viewers, so go2rtc can open this at will.
        url = self.hub.bridge_url + stream
        # JPEG frames cannot go over WebRTC: go2rtc's ffmpeg encodes H.264.
        return f"ffmpeg:{url}#video=h264" if self.app.get("transcode") else url

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        stale = time.monotonic() - self._fetched > CAMERA_MAX_AGE
        if stale and (self._task is None or self._task.done()):
            self._task = self.hass.async_create_background_task(self._fetch(), f"druckwerk camera {self.serial}")
        if (self._image is None or time.monotonic() - self._fetched > CAMERA_STALE) and self._task:
            # An old frame can show a state that no longer holds (light off), so wait for a new one.
            # The fetch continues if the caller times out; the card's next poll gets it.
            await asyncio.shield(self._task)
        return self._image
