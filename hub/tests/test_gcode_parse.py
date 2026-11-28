"""The 3D preview's G-code parser (hub/ui/views/gcodeparse.js), run with node on real slicer output and on small hand-made files.
Fixtures are the header and first layer cut from real Bambu Studio and OrcaSlicer files."""
import json
import os
import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
FIX = HERE / "fixtures" / "gcode"
# In CI (CI is set) a missing node must fail the test, not skip it.
pytestmark = pytest.mark.skipif(shutil.which("node") is None and not os.environ.get("CI"), reason="node is needed to run the browser parser")


def parse(text):
    out = subprocess.run(["node", str(HERE / "gcode_parse_harness.mjs")], input=text, capture_output=True, text=True, check=True, timeout=60)
    return json.loads(out.stdout)


def fixture(name):
    return (FIX / name).read_text(encoding="latin-1")


def test_bambu_plate_finds_every_object_and_the_h2s_plate():
    text = fixture("bambu_multi_object.gcode")
    labels = set(re.findall(r"; start printing object, unique label id: (\d+)", text))
    r = parse(text)
    assert len(labels) == 13 and r["objects"] == len(labels)
    assert r["plate"] == [[0, 0], [340, 0], [340, 320], [0, 320]]  # the H2S bed, not 256 x 256
    named = set(re.findall(r"^; FEATURE: (.+)$", text, re.M))
    shown = set(r["features"]) - {"Travel"}
    assert {"Outer wall", "Inner wall"} <= shown <= named


def test_orca_markers_count_each_object_once():
    # Orca writes the label marker and a named one for the same object
    text = fixture("orca_multi_object.gcode")
    assert len(re.findall(r"^; printing object ", text, re.M)) >= 13
    r = parse(text)
    assert r["objects"] == 13
    assert r["plate"] == [[0, 0], [256, 0], [256, 256], [0, 256]]
    assert "Brim" in r["features"]


def test_arc_fitted_file_has_no_objects_and_arcs_become_many_segments():
    text = fixture("bambu_arcs.gcode")
    arcs = len(re.findall(r"^G[23] .*E", text, re.M))
    r = parse(text)
    assert r["objects"] == 0 and arcs > 100
    assert r["segments"] > arcs * 2  # every extruding arc is more than one chord


@pytest.mark.parametrize("cmd,end,mid", [
    ("G3 X0 Y10 I-10 J0 E1", (0, 10), (7.071, 7.071)),    # counter-clockwise quarter, passes through 45 degrees
    ("G2 X0 Y-10 I-10 J0 E1", (0, -10), (7.071, -7.071)),  # clockwise from (10,0) around (0,0)
])
def test_arcs_follow_the_circle_in_the_right_direction(cmd, end, mid):
    r = parse(f"M83\n; CHANGE_LAYER\n; Z_HEIGHT: 0.2\n; FEATURE: Outer wall\nG1 X10 Y0 F9000\n{cmd}\n")
    pts = [(s[0], s[1]) for s in r["segs"]] + [(r["segs"][-1][3], r["segs"][-1][4])]
    assert len(r["segs"]) > 8
    assert all(abs(math.hypot(x, y) - 10) < 1e-6 for x, y in pts), "all vertices lie on the circle (no straight chord)"
    assert min(math.hypot(x - mid[0], y - mid[1]) for x, y in pts) < 1.0  # vertices are 1.3 mm apart here
    assert math.hypot(pts[-1][0] - end[0], pts[-1][1] - end[1]) < 1e-6


def test_full_circle_arc_is_a_circle():
    r = parse("M83\n; CHANGE_LAYER\n; Z_HEIGHT: 0.2\n; FEATURE: Outer wall\nG1 X10 Y0 F9000\nG3 X10 Y0 I-10 J0 E1\n")
    assert len(r["segs"]) > 30 and all(abs(math.hypot(s[0], s[1]) - 10) < 1e-6 for s in r["segs"])


def test_one_object_and_no_markers_is_zero_objects():
    r = parse("M83\n; CHANGE_LAYER\n; Z_HEIGHT: 0.2\n; FEATURE: Outer wall\nG1 X1 Y1\nG1 X5 Y1 E1\n")
    assert r["objects"] == 0 and r["segments"] == 1 and r["plate"] is None
