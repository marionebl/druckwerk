"""PUT /api/druckwerk/upload/<name>: secret check and ban hook in HA, everything else in the app."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from .conftest import HUB_SECRET, HUB_URL, calls, setup_entry

SECRET = "u" * 32
UPLOAD = "/api/druckwerk/upload/"
OK = {"X-Druckwerk-Secret": SECRET}
APP_UPLOAD = f"{HUB_URL}/api/upload/"


@pytest.fixture
async def client(hass, app, vendor_device, hass_client_no_auth):
    await setup_entry(hass, options={"upload_secret": SECRET})
    return await hass_client_no_auth()


def forwarded(app):
    return [c for c in app.mock_calls if c[0] == "PUT" and str(c[1]).startswith(APP_UPLOAD)]


async def test_a_good_upload_is_streamed_to_the_app_with_length_and_secret(client, app):
    seen = []

    async def answer(method, url, data):
        seen.append(await data.read())
        return AiohttpClientMockResponse(method, url, json={"file": "plate review.gcode.3mf", "bytes": 9, "sha256": "ab"})

    app.put(f"{APP_UPLOAD}plate%20review.gcode.3mf", side_effect=answer)
    resp = await client.put(UPLOAD + "plate review.gcode.3mf", data=b"123456789", headers=OK)
    assert resp.status == 200 and await resp.json() == {"file": "plate review.gcode.3mf", "bytes": 9, "sha256": "ab"}
    (put,) = forwarded(app)
    assert str(put[1]) == f"{APP_UPLOAD}plate%20review.gcode.3mf"  # the name is URL-encoded
    assert seen == [b"123456789"]
    assert put[3]["X-Hub-Secret"] == HUB_SECRET and put[3]["Content-Length"] == "9"
    assert "X-Druckwerk-Secret" not in put[3]


async def test_overwrite_is_passed_on_and_the_state_is_polled_so_the_select_lists_the_file(client, app):
    app.put(f"{APP_UPLOAD}a.gcode.3mf", json={"file": "a.gcode.3mf", "bytes": 1, "sha256": "x"})
    before = len(calls(app, "GET", "/api/integration/state"))
    resp = await client.put(UPLOAD + "a.gcode.3mf?overwrite=1", data=b"x", headers=OK)
    assert resp.status == 200
    assert forwarded(app)[0][1].query_string == "overwrite=1"
    assert len(calls(app, "GET", "/api/integration/state")) == before + 1


@pytest.mark.parametrize("status, text", [(409, "File exists; add ?overwrite=1 to replace it."),
                                          (400, "Body is not a 3MF (zip) file."), (411, "Content-Length is required.")])
async def test_an_app_error_is_passed_on_as_message(client, app, status, text):
    app.put(f"{APP_UPLOAD}a.gcode.3mf", status=status, json={"error": text})
    resp = await client.put(UPLOAD + "a.gcode.3mf", data=b"x", headers=OK)
    assert resp.status == status and await resp.json() == {"message": text}


async def test_a_wrong_or_missing_secret_is_401_and_counts_as_a_failed_login(client, app):
    with patch("custom_components.druckwerk.upload.process_wrong_login", new=AsyncMock()) as ban:
        assert (await client.put(UPLOAD + "a.gcode.3mf", data=b"x")).status == 401
        assert (await client.put(UPLOAD + "a.gcode.3mf", data=b"x", headers={"X-Druckwerk-Secret": "x" * 32})).status == 401
    assert ban.await_count == 2 and not forwarded(app)


async def test_uploads_are_off_without_a_secret(hass, app, vendor_device, hass_client_no_auth):
    await setup_entry(hass)
    client = await hass_client_no_auth()
    resp = await client.put(UPLOAD + "a.gcode.3mf", data=b"x", headers={"X-Druckwerk-Secret": ""})
    assert resp.status == 403 and not forwarded(app)


@pytest.mark.parametrize("name", ["a.stl", "a.gcode.3mf.exe", ".gcode.3mf", "-a.gcode.3mf", "x" * 120 + ".gcode.3mf"])
async def test_a_bad_name_is_400_before_anything_is_forwarded(client, app, name):
    resp = await client.put(UPLOAD + name, data=b"x", headers=OK)
    assert resp.status == 400 and "Name must be" in (await resp.json())["message"]
    assert not forwarded(app)


async def test_a_name_with_a_path_is_refused(client, app):
    resp = await client.put(UPLOAD + "..%2Fescape.gcode.3mf", data=b"x", headers=OK)
    assert resp.status in (400, 404) and not forwarded(app)


async def test_a_body_over_64_mb_is_413_before_it_is_read(client, app):
    resp = await client.put(UPLOAD + "a.gcode.3mf", data=b"x", headers={**OK, "Content-Length": str(64 * 1024 * 1024 + 1)})
    assert resp.status == 413 and (await resp.json())["message"] == "File too large (limit 67108864 bytes)."
    assert not forwarded(app)


async def test_a_body_without_a_length_is_411(client, app):
    async def chunks():
        yield b"x"

    resp = await client.put(UPLOAD + "a.gcode.3mf", data=chunks(), headers=OK)  # chunked
    assert resp.status == 411 and not forwarded(app)


async def test_an_unreachable_app_is_502(client, app):
    import aiohttp
    app.put(f"{APP_UPLOAD}a.gcode.3mf", exc=aiohttp.ClientError("down"))
    resp = await client.put(UPLOAD + "a.gcode.3mf", data=b"x", headers=OK)
    assert resp.status == 502 and "not reachable" in (await resp.json())["message"]


async def test_a_secret_with_a_byte_that_is_not_utf8_is_401_and_counts_as_a_failed_login(client, app):
    # aiohttp's client cannot send such a header, so write the request by hand
    reader, writer = await asyncio.open_connection(client.server.host, client.server.port)
    writer.write(b"PUT " + UPLOAD.encode() + b"a.gcode.3mf HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\n"
                 b"X-Druckwerk-Secret: ab\xff\xfecd\r\n\r\nx")
    with patch("custom_components.druckwerk.upload.process_wrong_login", new=AsyncMock()) as ban:
        status = (await reader.readline()).split()[1]
    writer.close()
    assert status == b"401" and ban.await_count == 1 and not forwarded(app)
