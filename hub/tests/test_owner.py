"""Ownership is a property of the printer: migration from owner grants, the setup field, and what an owner may do."""
import json

from hubfakes import ADMIN, SERIAL, USER
from queue_access import Grants
from queue_registry import Registry


def printers(tmp_path, **owners):
    reg = Registry(str(tmp_path))
    for s in ("S1", "S2"):
        reg.put(s, name=s, model="H2S", account="a", prefix=s.lower())
    for s, o in owners.items():
        reg.set_owner(s, o)
    return reg


def grants_file(tmp_path, items):
    (tmp_path / "grants.json").write_text(json.dumps(items))


def load(tmp_path, reg):
    return Grants(reg, str(tmp_path), lambda e: "on", lambda: 1e12)


def test_owner_grants_move_into_the_printer_and_other_grants_stay(tmp_path):
    reg = printers(tmp_path, S2="kept")
    grants_file(tmp_path, [
        {"id": "g1", "user_id": "u1", "printer": "S1", "role": "owner"},
        {"id": "g2", "user_id": "u2", "printer": "S2", "role": "owner", "needs_approval": True},  # S2 has another owner
        {"id": "g3", "user_id": "u3", "printer": "S1", "role": "user"},
        {"id": "g4", "user_id": "u9", "printer": "*", "role": "owner"}])  # all printers: S1 is u1's, S2 is kept's
    g = load(tmp_path, reg)
    assert (reg.get("S1")["owner"], reg.get("S2")["owner"]) == ("u1", "kept")
    by = {x["id"]: x for x in g.items}
    assert "g1" not in by  # moved, not copied
    assert by["g2"]["role"] == "user" and by["g2"]["needs_approval"] is True  # access stays, admin rights do not
    assert by["g3"]["role"] == "user" and by["g4"]["role"] == "user" and by["g4"]["printer"] == "*"
    assert [x["role"] for x in json.loads((tmp_path / "grants.json").read_text())] == ["user", "user", "user"]  # saved
    assert reg.get("S1")["owner"] == "u1" and load(tmp_path, Registry(str(tmp_path))).items == g.items  # a second load changes nothing


def test_a_wildcard_owner_grant_becomes_the_owner_of_every_printer_without_one(tmp_path):
    reg = printers(tmp_path)
    grants_file(tmp_path, [{"id": "g4", "user_id": "u9", "printer": "*", "role": "owner"}])
    g = load(tmp_path, reg)
    assert (reg.get("S1")["owner"], reg.get("S2")["owner"], g.items) == ("u9", "u9", [])


def test_a_wildcard_owner_grant_waits_while_no_printer_is_registered(tmp_path):
    reg = Registry(str(tmp_path))
    grants_file(tmp_path, [{"id": "g4", "user_id": "u9", "printer": "*", "role": "owner"}])
    g = load(tmp_path, reg)
    assert [x["id"] for x in g.items] == ["g4"]  # kept, so the later registration can still apply it
    reg.put("S1", name="S1", model="H2S", account="a", prefix="s1")
    g.migrate_owners()
    assert reg.get("S1")["owner"] == "u9" and g.items == []


def test_the_owner_role_cannot_be_granted_any_more(hub):
    hub.setup_printers()
    st, body = hub.req("POST", "/api/grants", {"user_id": USER, "printer": SERIAL, "role": "owner"}, user=ADMIN)
    assert st == 400 and "role" in body["error"]


def test_setup_edits_the_owner_and_an_owner_administers_the_printer(hub):
    hub.setup_printers()
    st, body = hub.req("PUT", "/api/setup/printers/" + SERIAL, {"name": "Lab", "owner": USER}, user=ADMIN)
    assert st == 200 and body["owner"] == USER, body
    assert [p["config"]["owner"] for p in hub.req("GET", "/api/setup", user=ADMIN)[1]["printers"] if p["serial"] == SERIAL] == [USER]
    assert hub.req("GET", "/api/me", user=USER)[1]["printers"] == [{"serial": SERIAL, "role": "admin"}]
    assert hub.req("PUT", "/api/setup/printers/" + SERIAL, {"name": "Lab"}, user=ADMIN)[1]["owner"] == USER  # no owner key: kept
    assert hub.req("PUT", "/api/setup/printers/" + SERIAL, {"name": "Lab", "owner": ""}, user=ADMIN)[1]["owner"] == ""
    assert hub.req("GET", "/api/me", user=USER)[1]["printers"] == []
    st, body = hub.req("PUT", "/api/setup/printers/" + SERIAL, {"name": "Lab", "owner": "nobody"}, user=ADMIN)
    assert st == 400 and "owner" in body["error"]
