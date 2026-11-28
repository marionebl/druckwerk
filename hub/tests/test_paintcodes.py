"""Painting helpers of New print (hub/ui/views/paintcodes.js), run with node: mesh refinement and the engine's paint codes."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# In CI (CI is set) a missing node must fail the test, not skip it.
pytestmark = pytest.mark.skipif(shutil.which("node") is None and not os.environ.get("CI"), reason="node is needed to run the browser module")


def run():
    out = subprocess.run(["node", str(Path(__file__).parent / "paintcodes_harness.mjs")], capture_output=True, text=True, check=True, timeout=60)
    return json.loads(out.stdout)


def test_subdivide_keeps_the_shape_and_quadruples_triangles_per_pass():
    r = run()
    assert r["tris"] == 16 and r["area"] == pytest.approx(32.0)


def test_refine_passes_follow_edge_length_and_cap():
    r = run()
    assert r["passes"] == 3  # an 11.3 mm edge down to 1.5 mm needs 3 halvings
    assert r["capped"] == 3  # 1 triangle * 4**3 = 64 <= 100, 4**4 = 256 is over the cap


def test_paint_codes_are_one_leaf_digit_per_painted_triangle():
    p = run()["paint"]
    assert p["triangles"] == [1, 2, 4] and p["codes"] == "484" and p["ends"] == [1, 2, 3] and p["kinds"] == ["supports"]


def test_an_unpainted_kind_is_left_out_so_the_engine_keeps_its_own_loader():
    assert run()["none"] is None
