"""Calls to the Druckwerk API and Supervisor discovery."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import aiohttp
from homeassistant.exceptions import HomeAssistantError

from .const import (
    BRIDGE_PORT,
    CONF_HUB_SECRET,
    CONF_HUB_URL,
    CONF_URL,
    HUB_PORT,
    ACT_AS_HEADER,
    HUB_SECRET_HEADER,
    HUB_SLUG_SUFFIX,
    SUPERVISOR_URL,
)

_TIMEOUT = aiohttp.ClientTimeout(total=10)


def hub_url_for(data: dict[str, Any]) -> str:
    """The hub API URL: explicit, else the bridge host on the hub port."""
    if data.get(CONF_HUB_URL):
        return str(data[CONF_HUB_URL]).rstrip("/")
    return f"http://{urlparse(data[CONF_URL]).hostname}:{HUB_PORT}"


class HubError(HomeAssistantError):
    """The hub refused or failed a call; the text is the hub's own (403 texts start with "rule:")."""

    def __init__(self, status: int, text: str) -> None:
        super().__init__(f"Druckwerk answered {status}: {text}")
        self.status = status
        self.text = text


async def hub_request(
    session: aiohttp.ClientSession,
    url: str,
    secret: str | None,
    method: str,
    path: str,
    body: Any = None,
    *,
    act_as: str | None = None,
    data: bytes | None = None,
    timeout: float | None = None,
    binary: bool = False,
    detail: bool = False,
) -> Any:
    """Call the hub; internal calls carry X-Hub-Secret.

    act_as applies that HA user's rules in the hub; None keeps the call admin (automations).
    detail raises HubError with the hub's error text instead of ClientResponseError, binary returns bytes.
    Raises ClientError/TimeoutError/ValueError otherwise.
    """
    headers = {HUB_SECRET_HEADER: secret} if secret else {}
    if act_as:
        headers[ACT_AS_HEADER] = act_as
    if data is not None:
        headers["Content-Type"] = "application/octet-stream"
    async with session.request(
        method,
        f"{url}{path}",
        json=body if data is None else None,
        data=data,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=timeout) if timeout else _TIMEOUT,
    ) as resp:
        if detail and resp.status >= 400:
            try:
                text = str((await resp.json(content_type=None)).get("error"))
            except (aiohttp.ClientError, ValueError, AttributeError):
                text = resp.reason or "error"
            raise HubError(resp.status, text)
        resp.raise_for_status()
        if binary:
            return await resp.read()
        return await resp.json(content_type=None)


async def _supervisor(session: aiohttp.ClientSession, path: str) -> Any:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        async with session.get(
            f"{SUPERVISOR_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            return (await resp.json(content_type=None)).get("data")
    except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError):
        return None


async def discover_bridge_url(session: aiohttp.ClientSession) -> str | None:
    """Bridge URL of the Druckwerk app; None off HA OS."""
    data = await _supervisor(session, "/addons")
    slugs = [a.get("slug", "") for a in (data or {}).get("addons", []) if isinstance(a, dict)]
    for slug in [s for s in slugs if s.endswith(HUB_SLUG_SUFFIX)]:
        info = await _supervisor(session, f"/addons/{slug}/info")
        host = (info or {}).get("hostname")
        if host:
            return f"http://{host}:{BRIDGE_PORT}"
    return None
