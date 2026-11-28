"""The plate of New print (hub/ui/views/plate.js), run with node: auto-orient, arrange, placement, binary STL."""
import functools
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# In CI (CI is set) a missing node must fail the test, not skip it.
pytestmark = pytest.mark.skipif(shutil.which("node") is None and not os.environ.get("CI"), reason="node is needed to run the browser module")


@functools.lru_cache(maxsize=1)
def run():
    out = subprocess.run(["node", str(Path(__file__).parent / "plate_harness.mjs")], capture_output=True, text=True, check=True, timeout=120)
    return json.loads(out.stdout)


@pytest.mark.parametrize("case", ["cube30x", "cube40y"])
def test_a_tilted_cube_comes_back_flat_and_square(case):
    r = run()[case]
    assert r["size"] == [20, 20, 20] and r["minZ"] == 0 and not r["identity"]


def test_a_cube_that_is_flat_already_is_left_alone():
    assert run()["cubeFlat"]["identity"] and run()["cube20"]["identity"]


def test_a_flat_plate_lies_flat_and_one_on_its_edge_is_laid_down():
    r = run()
    assert r["plateFlat"]["identity"] and r["plateFlat"]["size"][2] == 3
    assert r["plateOnEdge"]["size"][2] == 3 and r["plateTilted"]["size"][2] == 3


def test_a_pyramid_and_a_cone_stand_on_their_base():
    r = run()
    assert r["pyramid"]["identity"] and r["cone"]["identity"]  # given on the base: stays
    assert r["pyramidApexDown"]["size"] == [30, 30, 30] and not r["pyramidApexDown"]["identity"]  # given on the tip: turned over
    assert r["coneApexDown"]["size"] == [30, 30, 30] and not r["coneApexDown"]["identity"]


def test_a_mushroom_goes_cap_down_and_a_t_lies_flat():
    r = run()
    assert r["mushroom"]["base"] == pytest.approx(40, abs=0.5) and not r["mushroom"]["identity"]  # stem down becomes cap down
    assert r["mushroomCapDown"]["base"] == pytest.approx(40, abs=0.5) and r["mushroomCapDown"]["identity"]
    assert r["tshape"]["size"][2] <= 10  # no overhang under the bar, the T lies on its back


def test_the_test_plate_fixture_keeps_its_good_orientation():
    r = run()["fixture"]
    assert r["identity"] and r["size"][2] == 25


def test_orient_is_fast_on_a_big_mesh():
    r = run()
    assert r["bigTris"] >= 150000 and r["big"]["ms"] < 1000 and r["big"]["size"][2] == pytest.approx(20, abs=0.1)


def test_arrange_leaves_a_gap_and_stays_on_the_bed():
    r = run()
    assert r["arrange"] == {"overlaps": 0, "allInside": True, "allFit": True}
    assert r["single"] == [128, 128]  # one object sits in the middle of the bed
    assert r["keepOut"]  # nothing touches the keep-out box in a corner


def test_arrange_flags_what_does_not_fit():
    r = run()
    assert r["tooBig"] == {"long": False, "small": True, "smallInside": True}
    assert r["full"] == [True, False]  # two 200 mm squares: the second has no room
    assert r["tallFit"] is False  # higher than the printer


def test_placement_rests_on_z0_with_the_footprint_centred_on_at():
    p = run()["placed"]
    assert p["minZ"] == pytest.approx(0, abs=1e-4)
    assert p["centre"] == pytest.approx([50, 70], abs=1e-3)


def test_write_stl_round_trips_through_parse_stl():
    r = run()["roundTrip"]
    assert r["same"] and r["tris"] == r["want"] and r["bytes"] == 84 + 50 * r["want"]


def test_names_and_upload_name():
    r = run()
    assert r["names"] == ["a.stl", "b.stl", "a (2).stl", "a (3).stl"]
    assert r["upload"] == ["a.stl", "a + 2 more.stl"]
