"""Material, finish and colour name of a spool (hub/ui/views/spoolname.js), run with node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).parent.parent / "ui" / "views" / "spoolname.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the browser module")


def run(expr):
    code = f"import * as m from {json.dumps(MODULE.as_uri())}; console.log(JSON.stringify({expr}));"
    return json.loads(subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, check=True, timeout=30).stdout)


def d(brand, type_, name, colour):
    return run(f"m.describe({json.dumps(dict(brand=brand, type=type_, name=name, colour=colour))})")


def test_the_name_has_the_colour():
    assert d("Bambu Lab", "PLA", "PLA Matte Charcoal", "2E2E2E") == {"material": "PLA", "finish": "Matte", "colour": "Charcoal"}
    assert d("Acme", "PLA", "PLA Basic White", "F2F2F2") == {"material": "PLA", "finish": "Basic", "colour": "White"}
    assert d("Generic", "TPU", "TPU 95A Black", "222222") == {"material": "TPU", "finish": "Basic", "colour": "Black"}
    assert d("Sunlu", "PLA", "Sunlu PLA Ash Gray", "BBBBBB")["colour"] == "Ash Gray"
    assert d("Bambu Lab", "PLA", "PLA Translucent Orange", "F28A2E")["finish"] == "Translucent"


def test_a_name_without_colour_uses_the_hex():
    assert d("Bambu Lab", "PLA", "PLA Basic", "2B6CC4")["colour"] == "Blue"
    assert d("Bambu Lab", "PETG", "PETG HF", "E4572E") == {"material": "PETG", "finish": "HF", "colour": "Red"}
    assert d("Bambu Lab", "PLA", "PLA Basic", "2E2E2E")["colour"] == "Charcoal"  # exact table first


def test_nearest_basic_colour():
    got = run("['000000', 'FFFFFF', '7F7F7F', 'F2C94C', '3FB68B', 'F08FB8', '8B5A2B', 'E3D3B0', '', 'zz'].map((c) => m.colourName(c))")
    assert got == ["Black", "White", "Grey", "Yellow", "Green", "Pink", "Brown", "Beige", "Unknown", "Unknown"]
    assert run("[m.colourName('', 'Translucent'), m.colourName('11223300')]") == ["Transparent", "Transparent"]
