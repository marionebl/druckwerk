"""Contract of the integration towards dashboards, automations, Schaltpult, HomeKit and agents.

The golden file lists entity ids, actions and LLM tools. A change must be deliberate: run with
UPDATE_GOLDEN=1 to rewrite it and review the diff.
"""

import json
import os
from pathlib import Path

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
import yaml
import probatio

from custom_components.druckwerk import const
from custom_components.druckwerk.llm_api import API_ID

from .test_hub import hub_setup

GOLDEN = Path(__file__).parent / "golden" / "contract.json"


def _field(f: dict) -> dict:
    return {k: f[k] for k in ("required", "default") if k in f}


async def _snapshot(hass) -> dict:
    reg = er.async_get(hass)
    entities = sorted(
        (e.domain, e.unique_id, e.entity_id) for e in reg.entities.values() if e.platform == "druckwerk"
    )
    # The descriptions API imports the conversation component (hassil, turbojpeg); read the file it reads.
    desc = yaml.safe_load((Path(const.__file__).parent / "services.yaml").read_text())
    assert set(desc) == set(hass.services.async_services()["druckwerk"])
    services = {
        name: {"fields": {k: _field(v) for k, v in sorted((d.get("fields") or {}).items())}}
        for name, d in sorted(desc.items())
    }
    for name in services:  # response support is not in the descriptions
        services[name]["response"] = str(hass.services.supports_response("druckwerk", name))
    api = next(a for a in llm.async_get_apis(hass) if a.id == API_ID)
    inst = await api.async_get_api_instance(llm.LLMContext("test", None, None, None, None))
    tools = {}
    for t in sorted(inst.tools, key=lambda t: t.name):
        schema = probatio.to_openapi(t.parameters, custom_serializer=inst.custom_serializer, openapi_version="3.1.0")
        tools[t.name] = {"properties": sorted(schema.get("properties", {})), "required": sorted(schema.get("required", []))}
    return {
        "entities": [list(e) for e in entities],
        "services": services,
        "llm_api": {"id": API_ID, "tools": tools},
        "constants": {
            "event_prefix": const.EVENT_PREFIX,
            "upload_secret_header": const.UPLOAD_SECRET_HEADER,
            "upload_url": "/api/druckwerk/upload/{name}",
        },
    }


async def test_contract_matches_golden(hass, addon, media, vendor_device):
    await hub_setup(hass, mock=addon)
    snap = await _snapshot(hass)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n")
    assert snap == json.loads(GOLDEN.read_text())
