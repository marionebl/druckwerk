"""X-Druckwerk-Act-As: an internal call (secret) applies one HA user's rules, as for an ingress user."""
from hubfakes import ADMIN, OTHER, SERIAL, USER

ACT = "X-Druckwerk-Act-As"


def act(hub, method, path, body, user_id):
    return hub.req(method, path, body, secret=True, headers={ACT: user_id} if user_id else None)


def add(hub, user, name="a.gcode.3mf", target=SERIAL):
    return hub.req("POST", "/api/queue", {"file": name, "target": target}, user=user)[1]


def test_without_header_the_internal_caller_stays_admin(hub):
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    job = add(hub, USER)
    assert act(hub, "POST", "/api/queue/%s/skip" % job["id"], {}, None)[0] == 200
    assert act(hub, "GET", "/api/me", None, None)[1]["is_admin"] is True


def test_act_as_non_admin_cannot_touch_others_jobs(hub):
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    hub.grant(OTHER, SERIAL)
    theirs = add(hub, OTHER)["id"]
    for action in ("skip", "approve", "reject", "cancel"):
        st, body = act(hub, "POST", "/api/queue/%s/%s" % (theirs, action), {}, USER)
        assert st == 403 and body["error"].startswith("rule:"), (action, st, body)
    st, body = act(hub, "POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, USER)
    assert st == 403 and body["error"].startswith("rule:")
    st, body = act(hub, "POST", "/api/printers/%s/start" % SERIAL, {"job_id": theirs, "plate_clear": True}, USER)
    assert st == 403 and body["error"].startswith("rule:")
    assert act(hub, "POST", "/api/queue/%s/move" % theirs, {"target": SERIAL}, USER)[0] == 403


def test_act_as_user_can_cancel_own_job_and_add_with_grant(hub):
    hub.setup_printers()
    st, body = act(hub, "POST", "/api/queue", {"file": "m.gcode.3mf", "target": SERIAL}, USER)
    assert st == 403 and body["error"].startswith("rule:")  # no grant
    hub.grant(USER, SERIAL)
    st, job = act(hub, "POST", "/api/queue", {"file": "m.gcode.3mf", "target": SERIAL}, USER)
    assert st == 200 and job["owner_user_id"] == USER
    assert act(hub, "POST", "/api/queue/%s/cancel" % job["id"], {}, USER)[1]["state"] == "cancelled"


def test_act_as_user_sees_only_own_printers(hub):
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    assert [p["serial"] for p in act(hub, "GET", "/api/printers", None, USER)[1]] == [SERIAL]
    assert len(act(hub, "GET", "/api/printers", None, None)[1]) == 2


def test_act_as_ha_admin_can_do_admin_things(hub):
    hub.setup_printers()
    hub.seed_share()
    hub.grant(USER, SERIAL, needs_approval=True)
    job = add(hub, USER)
    assert job["state"] == "pending_approval"
    assert act(hub, "POST", "/api/queue/%s/approve" % job["id"], {}, ADMIN)[1]["state"] == "queued"
    hub.ha.s["sensor.lab_h2s_print_status"]["state"] = "idle"
    st, started = act(hub, "POST", "/api/printers/%s/start-next" % SERIAL, {"plate_clear": True}, ADMIN)
    assert st == 200 and started["id"] == job["id"]
    assert act(hub, "POST", "/api/queue/%s/skip" % add(hub, USER, "b.gcode.3mf")["id"], {}, ADMIN)[0] == 200


def test_act_as_printer_owner_is_admin_of_that_printer_only(hub):
    hub.setup_printers()
    hub.grant(USER, SERIAL, role="owner")
    mine = add(hub, ADMIN, "m.gcode.3mf", SERIAL)["id"]
    other = add(hub, ADMIN, "o.gcode.3mf", "FAKE0002")["id"]
    assert act(hub, "POST", "/api/queue/%s/skip" % mine, {}, USER)[0] == 200
    assert act(hub, "POST", "/api/queue/%s/skip" % other, {}, USER)[0] == 403


def test_unknown_or_inactive_user_is_403_with_a_rule(hub):
    hub.setup_printers()
    for uid in ("nobody", "sys-not-listed"):
        st, body = act(hub, "GET", "/api/queue", None, uid)
        assert st == 403 and body["error"].startswith("rule: act-as user"), body
        assert act(hub, "POST", "/api/queue", {"file": "a.gcode.3mf", "target": SERIAL}, uid)[0] == 403
    hub.ha.users = [dict(u, is_active=False) if u["id"] == USER else u for u in hub.ha.users]
    hub.app._cache.clear()
    assert act(hub, "GET", "/api/queue", None, USER)[0] == 403


def test_act_as_is_ignored_without_the_secret(hub):
    hub.setup_printers()
    # An ingress user cannot become someone else, and the header alone is no identity.
    assert hub.req("GET", "/api/queue", user=USER, headers={ACT: ADMIN})[1]["jobs"] == []
    assert hub.req("GET", "/api/me", user=USER, headers={ACT: ADMIN})[1]["is_admin"] is False
    assert hub.req("GET", "/api/queue", headers={ACT: ADMIN})[0] == 401
    assert hub.req("GET", "/api/queue", headers={ACT: ADMIN, "X-Hub-Secret": "wrong"})[0] == 401


def test_act_as_on_slicer_routes_uses_the_users_ownership(hub):
    hub.setup_printers()
    jid = hub.sliced(owner=USER)
    assert act(hub, "GET", "/api/jobs/" + jid, None, USER)[0] == 200
    assert act(hub, "GET", "/api/jobs/" + jid, None, OTHER)[0] == 403
    assert act(hub, "GET", "/api/jobs/" + jid, None, ADMIN)[0] == 200


def test_slice_job_answer_has_a_cost_estimate(hub):
    hub.setup_printers()
    hub.app.spools.add(brand="X", type="PETG", colour="ADB1B2", name="n", grams_left=900, grams_total=1000,
                       price_per_kg=20.0, printer=SERIAL, unit=0, slot=0)
    jid = hub.sliced(owner=USER)
    job = act(hub, "GET", "/api/jobs/" + jid, None, USER)[1]
    # 75.22 g at 20/kg plus 7440 s of electricity at the printer's price
    assert job["cost_estimate"] > 1.5 and job["cost_estimate"] == round(job["cost_estimate"], 2)
