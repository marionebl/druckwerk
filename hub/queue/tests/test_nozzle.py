import json
import os

import pytest

from queue_engine import QueueError
from queue_registry import Registry


def test_an_old_single_nozzle_becomes_the_first_library_entry(tmp_path):
    old = {"S1": {"name": "One", "model": "H2S", "account": "a", "prefix": "one", "plate_type": "", "nozzle": "0.6",
                  "pause_entities": [], "electricity_kwh_price": "", "owner": ""},
           "S2": {"name": "Two", "model": "P1S", "account": "a", "prefix": "two", "plate_type": "", "nozzle": "",
                  "pause_entities": [], "electricity_kwh_price": "", "owner": ""}}
    (tmp_path / "printers.json").write_text(json.dumps(old))
    reg = Registry(str(tmp_path))
    assert reg.get("S1")["nozzles"] == [{"diameter": "0.6", "type": ""}] and "nozzle" not in reg.get("S1")
    assert reg.get("S2")["nozzles"] == []
    assert "nozzle" not in json.load(open(tmp_path / "printers.json"))["S1"]  # migrated once, on disk
    assert Registry(str(tmp_path)).get("S1")["nozzles"] == [{"diameter": "0.6", "type": ""}]


def test_put_checks_and_dedupes_the_library(tmp_path):
    reg = Registry(str(tmp_path))
    n = [{"diameter": "0.4", "type": "hardened_steel"}, {"diameter": "0.4", "type": "hardened_steel"}, {"diameter": "0.2", "type": ""}]
    assert reg.put("S1", name="One", model="H2S", prefix="one", nozzles=n)["nozzles"] == [n[0], n[2]]
    for bad in ("0.4", [{"diameter": "0.5"}], [{"diameter": "0.4", "type": "Hardened Steel"}], ["0.4"]):
        with pytest.raises(ValueError):
            reg.put("S1", name="One", model="H2S", prefix="one", nozzles=bad)


def with_nozzle(make, installed, need):
    make.get_nozzle = lambda serial: installed
    job = make.add("a.gcode.3mf", target="S1")
    job["nozzle"] = need
    return job


def test_start_is_refused_for_another_nozzle(make, fake):
    job = with_nozzle(make, "0.4", "0.6")
    with pytest.raises(QueueError, match=r"^This job needs a 0\.6 mm nozzle; the printer has 0\.4 mm\.$"):
        make.start("S1", plate_clear=True)
    assert job["state"] == "queued" and not [c for c in fake.calls if c[1] == "print_file"]


@pytest.mark.parametrize("installed, need", [("0.6", "0.6"), ("0.60", "0.6"), (None, "0.6"), ("0.8", None)])
def test_start_is_allowed_when_it_matches_or_nothing_is_known(make, installed, need):
    job = with_nozzle(make, installed, need)
    assert make.start("S1", plate_clear=True)["id"] == job["id"]
