import copy
import json
import pathlib
from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

pytest_plugins = "pytest_homeassistant_custom_component"

# The plugin ships its own custom_components package that shadows ours; add our folder to it.
import custom_components

_ours = str(pathlib.Path(__file__).parents[1] / "custom_components")
if _ours not in list(custom_components.__path__):
    custom_components.__path__.append(_ours)

from custom_components.druckwerk.externals import VENDOR_DOMAIN, VENDOR_MANUFACTURER  # noqa: E402

URL = "http://local-druckwerk:8098"
HUB_URL = "http://local-druckwerk:8099"
HUB_SECRET = "s" * 32
SERIAL = "0000TEST0000001"
SERIAL2 = "FAKE0002"
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
# Both come from the real app code (see the fixtures' generator in the commit message).
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
STATE = json.loads((FIXTURES / "state.json").read_text())


def state_doc(*serials, revision=None, **changes):
    """The fixture state with only these printers (default: the first one).

    changes is {serial: {entity key: value}} merged over the fixture's entity values.
    """
    doc = copy.deepcopy(STATE)
    doc["printers"] = {s: doc["printers"][s] for s in serials or (SERIAL,)}
    if revision:
        doc["manifest_revision"] = revision
    for serial, entities in changes.items():
        doc["printers"][serial]["entities"].update(entities)
    return doc


def mock_hub(mock, state=None, manifest=MANIFEST):
    """Mock the app's routes. The first matching mock wins, so drop the earlier hub mocks."""
    mock._mocks = [m for m in mock._mocks if not str(m.url).startswith(HUB_URL)]
    state = state if state is not None else state_doc()
    mock.get(f"{HUB_URL}/api/integration/state", json=dict(state, manifest_revision=manifest["revision"]))
    mock.get(f"{HUB_URL}/api/integration/manifest", json=manifest)
    mock.put(f"{HUB_URL}/api/integration/options", json={"recipients": {}})
    mock.get(f"{URL}/api/health", json={"server": "ok"})


def calls(mock, method, suffix):
    """The mocked requests of a method whose URL ends with suffix: (method, url, data, headers)."""
    return [c for c in mock.mock_calls if c[0] == method and str(c[1]).endswith(suffix)]


@pytest.fixture(autouse=True)
def _custom(enable_custom_integrations):
    yield


@pytest.fixture
def app(aioclient_mock):
    mock_hub(aioclient_mock)
    return aioclient_mock


@pytest.fixture
def vendor_device(hass):
    """The device the vendor integration would have registered."""
    entry = MockConfigEntry(domain=VENDOR_DOMAIN)
    entry.add_to_hass(hass)
    from homeassistant.helpers import device_registry as dr
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(VENDOR_DOMAIN, SERIAL)},
        name="My H2S", manufacturer=VENDOR_MANUFACTURER, model="H2S",
    )


async def setup_entry(hass, options=None, data=None, **kwargs):
    entry = MockConfigEntry(
        domain="druckwerk", unique_id="druckwerk", options=options or {},
        data=data if data is not None else {"url": URL, "hub_secret": HUB_SECRET}, **kwargs,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def tick(hass, seconds=6):
    """Let the state poll run once."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def with_entities(**changes):
    """The fixture manifest with another revision; changes is {key: descriptor or None to drop it}."""
    doc = copy.deepcopy(MANIFEST)
    doc["revision"] = "changed000001"
    doc["entities"] = [e for e in doc["entities"] if changes.get(e["key"], 1) is not None]
    doc["entities"] += [dict(d, key=k) for k, d in changes.items() if d]
    return doc
