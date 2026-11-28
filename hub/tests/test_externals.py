"""externals.env (read by shell scripts) must agree with externals.py (read by Python)."""
import os
import re
import shlex
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import externals  # noqa: E402

ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "externals.env")


def read_env():
    out = {}
    for line in open(ENV):
        if line.strip() and not line.startswith("#"):
            k, _, v = line.rstrip("\n").partition("=")
            out[k] = shlex.split(v)[0] if v else ""
    return out


def test_env_matches_python():
    e = read_env()
    assert e["PLUGIN_NET_LIB"] == externals.PLUGIN_NET_LIB
    assert e["PLUGIN_FIND_PATTERNS"].split() == list(externals.PLUGIN_FIND_PATTERNS)
    assert e["BRIDGE_LIB_ENV"] == externals.BRIDGE_LIB_ENV
    assert e["PLUGIN_AGENT_DIR"] == externals.PLUGIN_AGENT_DIR
    assert e["CERT_URL"] == externals.CERT_URL
    assert e["CERT_SHA256"] == externals.CERT_SHA256


def test_plugin_net_lib_is_one_of_the_libs():
    assert externals.PLUGIN_NET_LIB in externals.PLUGIN_LIBS


def test_every_nozzle_has_three_qualities_and_its_own_machine_preset():
    for model, cfg in externals.PRINTERS.items():
        assert set(cfg["quality"]) == {"good", "better", "best"}
        for d, n in cfg["nozzles"].items():
            assert d != "0.4" and n["preset"] == "Bambu Lab %s %s nozzle" % (model, d)  # 0.4 is the model's own entry
            assert set(n["quality"]) == {"good", "better", "best"}
            assert all(q.endswith("%s nozzle" % d) for q in n["quality"].values()), (model, d)


# The vendor name may stand only in the files and lines below; everything else says "printer cloud" or "vendor slicer".
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
VENDOR_FILES = re.compile(r"hub/externals\.(py|env)|custom_components/druckwerk/(externals\.py|manifest\.json)"
                          r"|hub/tests/(test_externals\.py|fixtures/gcode/.*)|hub/CHANGELOG\.md")  # the changelog is a record
VENDOR_LINES = re.compile(r"ha-bambulab|pybambu|bambu_lab|bambu_networking|bambulab\.com"  # project, HA domain, external names, URLs
                          r"|Bambu Lab (H2S|P1S|P1P|X1|A1)|\"Bambu Lab\"|Bambu (PLA|PETG)")  # data values: models, presets, brand


def test_the_vendor_name_stands_only_in_allowed_places():
    try:
        out = subprocess.run(["git", "grep", "-i", "-n", "bambu"], cwd=REPO, capture_output=True, text=True, check=False).stdout
    except OSError:
        pytest.skip("git is not available")
    hits = []
    for line in out.splitlines():
        path, _, rest = line.split(":", 2)
        if not VENDOR_FILES.fullmatch(path) and not VENDOR_LINES.search(rest):
            hits.append(line[:160])
    assert not hits, "avoidable vendor name:\n" + "\n".join(hits)
