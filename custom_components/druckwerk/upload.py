"""PUT /api/druckwerk/upload/<name>.gcode.3mf: forwards a sliced file to the app's print file list.

HA's own auth is off for this view; a shared secret from the integration options guards it,
so agents without an HA account can upload. A wrong secret counts as a failed login (IP ban).
The app checks the content and stores the file.
"""

from __future__ import annotations

import hmac
import re
from urllib.parse import quote

import aiohttp
from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.components.http.ban import process_wrong_login

from .const import CONF_UPLOAD_SECRET, DOMAIN, MAX_UPLOAD_BYTES, UPLOAD_SECRET_HEADER, UPLOAD_TIMEOUT
from .hub_api import HubError

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,118}\.gcode\.3mf")


class UploadView(HomeAssistantView):
    url = "/api/druckwerk/upload/{name}"
    name = "api:druckwerk:upload"
    requires_auth = False

    async def put(self, request: web.Request, name: str) -> web.Response:
        hass = request.app[KEY_HASS]
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        secret = entries[0].options.get(CONF_UPLOAD_SECRET) if entries else None
        if not secret:
            return self.json_message("Uploads are off: set an upload secret in the Druckwerk options.", 403)
        given = request.headers.get(UPLOAD_SECRET_HEADER, "")
        if not hmac.compare_digest(given.encode(), secret.encode()):
            await process_wrong_login(request)
            return self.json_message("Wrong or missing upload secret.", 401)
        if not NAME_RE.fullmatch(name):
            return self.json_message("Name must be letters, digits, '._ -' and end in .gcode.3mf.", 400)
        # The app needs a length and answers too large only after it read the body, so check here.
        size = request.content_length
        if size is None:
            return self.json_message("Content-Length is required.", 411)
        if size > MAX_UPLOAD_BYTES:
            return self.json_message("File too large (limit %d bytes)." % MAX_UPLOAD_BYTES, 413)

        hub = entries[0].runtime_data
        path = f"/api/upload/{quote(name, safe='')}" + ("?overwrite=1" if request.query.get("overwrite") == "1" else "")
        try:
            doc = await hub.call(
                "PUT", path, data=request.content, headers={"Content-Length": str(size)}, timeout=UPLOAD_TIMEOUT
            )
        except HubError as err:
            return self.json_message(err.text, err.status)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            return self.json_message(f"Druckwerk not reachable: {err}", 502)
        await hub.async_request_refresh()  # the select lists the new file
        return self.json(doc)

    post = put
