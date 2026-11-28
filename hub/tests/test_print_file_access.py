"""The action print_file applies the act-as user's rules: only an admin of the printer prints directly, and only an admin forces."""
import pytest

from hubfakes import ADMIN, SERIAL, USER, FakeBridge, make_3mf

ACT = "X-Druckwerk-Act-As"


def print_file(hub, user_id, **fields):
    body = dict({"serial": SERIAL, "file": "b.gcode.3mf"}, **fields)
    return hub.req("POST", "/api/actions/print_file", body, secret=True, headers={ACT: user_id} if user_id else None)


@pytest.fixture
def hub(hub):
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    make_3mf(hub.media / "druckwerk" / "b.gcode.3mf")
    return hub


def test_user_with_a_grant_cannot_print_or_force_on_a_running_printer(hub):
    FakeBridge.printer_extra = {SERIAL: {"print_status": "RUNNING"}}
    for fields in ({}, {"force": True}):
        status, body = print_file(hub, USER, **fields)
        assert status == 403 and body["error"].startswith("rule:"), (fields, status, body)
    FakeBridge.printer_extra = {}
    assert print_file(hub, USER)[0] == 403  # a grant is not enough on an idle printer either
    assert hub.sent() == []


def test_admin_and_printer_owner_print_and_force(hub):
    hub.owner(USER, SERIAL)
    FakeBridge.printer_extra = {SERIAL: {"print_status": "RUNNING"}}
    assert print_file(hub, USER)[0] == 409  # allowed, then refused by the busy check
    assert print_file(hub, ADMIN, force=True)[0] == 200
    assert print_file(hub, USER, force=True)[0] == 200
    assert len(hub.sent()) == 2


def test_call_without_a_user_stays_admin(hub):
    assert print_file(hub, None)[0] == 200 and len(hub.sent()) == 1
