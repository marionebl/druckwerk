"""An empty HUB_INGRESS_IPS must not turn every network client into a trusted ingress gateway."""
import importlib.util
import os

import pytest

from hubfakes import ADMIN, REPO

DEFAULT = ["172.30.32.2"]


@pytest.fixture
def hub_py(hub):  # the hub fixture puts slicer_server into sys.modules, which hub.py imports
    spec = importlib.util.spec_from_file_location("hub_main", os.path.join(REPO, "hub", "hub.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("value", [None, "", ",", " , "])
def test_empty_variable_uses_the_default_address(hub_py, monkeypatch, value):
    monkeypatch.delenv("HUB_INGRESS_IPS", raising=False)
    if value is not None:
        monkeypatch.setenv("HUB_INGRESS_IPS", value)
    assert hub_py.ingress_ips() == DEFAULT


def test_list_is_kept(hub_py, monkeypatch):
    monkeypatch.setenv("HUB_INGRESS_IPS", "10.0.0.1, 10.0.0.2")
    assert hub_py.ingress_ips() == ["10.0.0.1", "10.0.0.2"]


def test_forged_identity_from_another_address_gets_401(hub, hub_py, monkeypatch):
    monkeypatch.setenv("HUB_INGRESS_IPS", "")
    hub.app.trusted_ips = set(hub_py.ingress_ips())  # the test client connects from 127.0.0.1
    assert hub.req("GET", "/api/me", user=ADMIN)[0] == 401
