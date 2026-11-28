"""POST /api/feedback (the Claude feedback button) and POST /api/ha/switches (the Create switch button of the grant form)."""
import json

from ha import HAError
from hubfakes import ADMIN, SERIAL, USER


def post_feedback(hub, body, user=USER):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return hub.req("POST", "/api/feedback", raw=raw, user=user, headers={"Content-Type": "application/json"})


def test_feedback_adds_a_todo_item_for_any_ha_user(hub):
    item, description = "Feedback 2026-11-27 10:00:00Z · druckwerk #/access · too small", json.dumps({"message": "too small"})
    for user in (USER, ADMIN):
        hub.ha.calls.clear()
        assert post_feedback(hub, {"item": item, "description": description}, user) == (200, {"ok": True})
        assert hub.ha.calls == [("todo", "add_item", {"entity_id": "todo.feedback", "item": "%s · from %s" % (item, user.upper()), "description": description})]


def test_feedback_needs_a_home_assistant_identity(hub):
    st, _ = hub.req("POST", "/api/feedback", {"item": "x", "description": "{}"})
    assert st == 401 and hub.ha.calls == []


def test_feedback_rejects_bad_bodies_with_400(hub):
    bad = [b"", b"not json", b"[]", b"{}", b'{"item": "x"}', b'{"description": "{}"}',
           json.dumps({"item": "", "description": "{}"}).encode(), json.dumps({"item": "x" * 301, "description": "{}"}).encode(),
           json.dumps({"item": 5, "description": "{}"}).encode(), json.dumps({"item": "x", "description": ""}).encode(),
           json.dumps({"item": "x", "description": {"a": 1}}).encode()]
    for body in bad:
        st, out = post_feedback(hub, body)
        assert st == 400 and "error" in out, (body, st, out)
    assert hub.ha.calls == []
    assert post_feedback(hub, {"item": "x" * 300, "description": "{}"})[0] == 200  # 300 characters is the limit


def test_feedback_accepts_a_description_up_to_300_kb(hub):
    big = json.dumps({"message": "m", "screenshot": "a" * 250_000})
    assert post_feedback(hub, {"item": "x", "description": big})[0] == 200
    assert hub.ha.calls[-1][2]["description"] == big
    hub.ha.calls.clear()
    st, out = post_feedback(hub, {"item": "x", "description": "a" * 300_001})
    assert st == 400 and "300000" in out["error"] and hub.ha.calls == []


def test_feedback_item_carries_the_display_name_of_the_caller(hub):
    post_feedback(hub, {"item": "Feedback · #/queue · slow", "description": "{}"}, USER)
    assert hub.ha.calls[-1][2]["item"] == "Feedback · #/queue · slow · from %s" % USER.upper()


def test_feedback_is_limited_to_10_items_per_user_per_hour(hub):
    for _ in range(10):
        assert post_feedback(hub, {"item": "x", "description": "{}"}, USER)[0] == 200
    st, out = post_feedback(hub, {"item": "x", "description": "{}"}, USER)
    assert st == 429 and "10" in out["error"] and len(hub.ha.calls) == 10
    assert post_feedback(hub, {"item": "x", "description": "{}"}, ADMIN)[0] == 200  # the limit is per user


def test_feedback_limit_frees_up_after_an_hour(hub):
    now = [1000.0]
    hub.app.clock = lambda: now[0]
    for _ in range(10):
        post_feedback(hub, {"item": "x", "description": "{}"}, USER)
    assert post_feedback(hub, {"item": "x", "description": "{}"}, USER)[0] == 429
    now[0] += 3601
    assert post_feedback(hub, {"item": "x", "description": "{}"}, USER)[0] == 200


def test_json_routes_need_the_json_content_type(hub):
    """A cross-site form can send text/plain without a preflight; the body of a JSON route must say it is JSON."""
    body = json.dumps({"item": "x", "description": "{}"}).encode()
    for ctype in ({"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"}, {"Content-Type": ""}):
        st, out = hub.req("POST", "/api/feedback", raw=body, user=USER, headers=ctype)
        assert st == 400 and "Content-Type" in out["error"], (ctype, st, out)
    grant = json.dumps({"user_id": USER, "printer": "*"}).encode()
    st, out = hub.req("POST", "/api/grants", raw=grant, user=ADMIN, headers={"Content-Type": "text/plain"})
    assert st == 400 and "Content-Type" in out["error"]
    assert hub.ha.calls == []
    st, _ = hub.req("POST", "/api/grants", raw=grant, user=ADMIN, headers={"Content-Type": "application/json; charset=utf-8"})
    assert st == 200


def test_feedback_other_routes_keep_the_small_body_limit(hub):
    st, _ = hub.req("POST", "/api/grants", raw=json.dumps({"user_id": USER, "printer": "*", "pad": "a" * 20000}).encode(), user=ADMIN)
    assert st == 400


def test_feedback_reports_an_unreachable_ha_as_502(hub):
    def down(*a, **k):
        raise HAError("down")
    hub.ha.call_service = down
    st, out = post_feedback(hub, {"item": "x", "description": "{}"})
    assert st == 502 and out["error"] == "down"


# -- Create switch --
def test_create_switch_makes_an_input_boolean_and_returns_its_entity_id(hub):
    hub.setup_printers()
    st, out = hub.req("POST", "/api/ha/switches", {"user_id": USER, "printer": SERIAL}, user=ADMIN)
    assert st == 200, out
    assert out == {"entity_id": "input_boolean.druckwerk_ben_lab", "name": "Druckwerk Ben Lab", "state": "off"}
    assert hub.ha.commands == [("input_boolean/create", {"name": "Druckwerk Ben Lab", "icon": "mdi:printer-3d"})]


def test_create_switch_for_all_printers(hub):
    hub.setup_printers()
    st, out = hub.req("POST", "/api/ha/switches", {"user_id": USER, "printer": "*"}, user=ADMIN)
    assert st == 200 and out["name"] == "Druckwerk Ben All printers" and out["entity_id"] == "input_boolean.druckwerk_ben_all_printers"


def test_grant_accepts_the_new_switch_before_it_is_in_the_states(hub):
    hub.setup_printers()
    eid = hub.req("POST", "/api/ha/switches", {"user_id": USER, "printer": SERIAL}, user=ADMIN)[1]["entity_id"]
    assert eid not in hub.ha.states()
    st, g = hub.req("POST", "/api/grants", {"user_id": USER, "printer": SERIAL, "condition_entity": eid}, user=ADMIN)
    assert st == 200 and g["condition_entity"] == eid and g["active"] is False  # off until someone turns it on


def test_create_switch_is_for_ha_admins_like_grants(hub):
    hub.setup_printers()
    st, out = hub.req("POST", "/api/ha/switches", {"user_id": USER, "printer": SERIAL}, user=USER)
    assert st == 403 and out["error"].startswith("rule:") and hub.ha.commands == []
    assert hub.req("POST", "/api/ha/switches", {"user_id": USER, "printer": SERIAL})[0] == 401
    assert hub.req("POST", "/api/ha/switches", {"user_id": USER, "printer": SERIAL}, secret=True)[0] == 200  # internal, like grants


def test_create_switch_rejects_unknown_user_or_printer(hub):
    hub.setup_printers()
    for body in ({"user_id": "nobody", "printer": SERIAL}, {"user_id": USER, "printer": "NOPE"}, {"printer": SERIAL}, {"user_id": USER}):
        st, out = hub.req("POST", "/api/ha/switches", body, user=ADMIN)
        assert st == 400 and "error" in out, (body, st, out)
    assert hub.ha.commands == []
