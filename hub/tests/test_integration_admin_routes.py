"""The chamber light and the AMS re-read of the integration apply the act-as user's rules (owner decision D2)."""
import pytest

from hubfakes import ADMIN, SERIAL, USER

ACT = "X-Druckwerk-Act-As"
ROUTES = (("light", {"on": True}), ("rfid", {"ams_id": 0, "slot_id": 1}))


def post(hub, route, body, user_id):
    return hub.req("POST", "/api/integration/printers/%s/%s" % (SERIAL, route), body, secret=True,
                   headers={ACT: user_id} if user_id else None)


@pytest.fixture
def hub(hub):
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    return hub


def test_a_user_with_only_a_grant_is_refused(hub):
    for route, body in ROUTES:
        status, answer = post(hub, route, body, USER)
        assert status == 403 and answer["error"].startswith("rule:"), (route, status, answer)


def test_admin_owner_and_automation_are_allowed(hub):
    hub.owner(USER, SERIAL)
    for route, body in ROUTES:
        for user_id in (ADMIN, USER, None):
            assert post(hub, route, body, user_id)[0] == 200, (route, user_id)
