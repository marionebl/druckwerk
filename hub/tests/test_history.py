"""Print history: Bambu cloud tasks are the source of truth, Druckwerk jobs only attribute them to a user."""
from hubfakes import ADMIN, OTHER, SERIAL, SERIAL2, USER, FakeBridge, task


def job(hub, id, file, owner, started, printer=SERIAL, state="done", cost=1.5):
    j = {"id": id, "file": file, "source": file, "target": printer, "model": "", "filaments": [], "options": {}, "error": None,
         "state": state, "owner_user_id": owner, "owner_name": (owner or "").upper(), "created_at": started - 5,
         "printer": printer, "started_at": started, "finished_at": started + 600, "cost": cost}
    hub.app.queue.history.append(j)
    return j


def scene(hub):
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    hub.grant(OTHER, SERIAL)
    FakeBridge.tasks = {SERIAL: [task(3, "cube", 9000), task(2, "vase", 6000), task(1, "cube", 5000)],
                        SERIAL2: [task(4, "benchy", 7000, SERIAL2)]}
    job(hub, "j1", "cube.gcode.3mf", USER, 4950)
    job(hub, "j2", "cube.gcode.3mf", OTHER, 8990)  # same file twice: each job takes the task closest after its start
    job(hub, "j3", "benchy.gcode.3mf", ADMIN, 6990, SERIAL2, cost=2.5)


def items(hub, user, query=""):
    st, body = hub.req("GET", "/api/history" + query, user=user)
    assert st == 200, body
    return body["items"]


def test_admin_sees_everything_newest_first_with_attribution(hub):
    scene(hub)
    got = items(hub, ADMIN)
    assert [i["id"] for i in got] == [3, 4, 2, 1]
    by = {i["id"]: i for i in got}
    assert (by[1]["user_id"], by[1]["user_name"], by[1]["job_id"], by[1]["cost"], by[1]["source"]) == (USER, USER.upper(), "j1", 1.5, "druckwerk")
    assert (by[3]["user_id"], by[3]["job_id"]) == (OTHER, "j2")
    assert (by[4]["user_id"], by[4]["cost"], by[4]["serial"]) == (ADMIN, 2.5, SERIAL2)
    assert by[1]["title"] == "cube" and by[1]["serial"] == SERIAL and by[1]["printer_name"] == "Lab"
    assert (by[2]["user_id"], by[2]["user_name"], by[2]["job_id"], by[2]["cost"], by[2]["source"]) == (None, None, None, None, "cloud")


def test_admin_filters_by_user_unknown_and_printer(hub):
    scene(hub)
    assert [i["id"] for i in items(hub, ADMIN, "?user=" + USER)] == [1]
    assert [i["id"] for i in items(hub, ADMIN, "?user=unknown")] == [2]
    assert [i["id"] for i in items(hub, ADMIN, "?printer=" + SERIAL2)] == [4]
    assert [i["id"] for i in items(hub, ADMIN, "?limit=2")] == [3, 4]
    assert hub.req("GET", "/api/history?limit=x", user=ADMIN)[0] == 400


def test_user_sees_only_own_items_and_cannot_widen(hub):
    scene(hub)
    assert [i["id"] for i in items(hub, USER)] == [1]
    assert [i["id"] for i in items(hub, USER, "?user=" + OTHER)] == [1]  # the filter is ignored, not obeyed
    assert [i["id"] for i in items(hub, USER, "?user=unknown")] == [1]
    assert [i["id"] for i in items(hub, USER, "?printer=" + SERIAL2)] == []
    assert [i["id"] for i in items(hub, OTHER)] == [3]


def test_cloud_only_task_is_hidden_from_users_and_has_no_user_for_admin(hub):
    scene(hub)
    assert 2 not in [i["id"] for i in items(hub, USER)] + [i["id"] for i in items(hub, OTHER)]
    assert next(i for i in items(hub, ADMIN) if i["id"] == 2)["user_id"] is None


def test_printer_owner_sees_all_on_that_printer_only(hub):
    scene(hub)
    hub.grant("u-owner", SERIAL, role="owner")
    hub.ha.users.append({"id": "u-owner", "name": "Olga", "is_owner": False, "is_active": True, "system_generated": False,
                         "group_ids": ["system-users"]})
    assert [i["id"] for i in items(hub, "u-owner")] == [3, 2, 1]
    assert [i["id"] for i in items(hub, "u-owner", "?user=" + USER)] == [1]


def test_no_grant_no_history(hub):
    scene(hub)
    hub.ha.users.append({"id": "u-new", "name": "Nia", "is_owner": False, "is_active": True, "system_generated": False,
                         "group_ids": ["system-users"]})
    assert items(hub, "u-new") == []


def test_job_and_task_must_agree_on_printer_name_and_time(hub):
    hub.setup_printers()
    FakeBridge.tasks = {SERIAL: [task(1, "cube", 5000), task(2, "cube", 20000), task(3, "gear", 30000)]}
    job(hub, "far", "cube.gcode.3mf", USER, 5000 - 7200)  # task starts 2 h after the job: not the same print
    job(hub, "name", "cube.gcode.3mf", USER, 29990)  # right time, other file
    job(hub, "ok", "Cube.3mf", OTHER, 19990)  # extensions and case do not matter
    got = {i["id"]: i for i in items(hub, ADMIN)}
    assert [got[1]["user_id"], got[2]["user_id"], got[3]["user_id"]] == [None, OTHER, None]


def test_cloud_is_the_source_of_truth_jobs_without_a_task_do_not_appear(hub):
    scene(hub)
    job(hub, "ghost", "ghost.gcode.3mf", USER, 99999)
    assert "ghost" not in [i["job_id"] for i in items(hub, ADMIN)]


def test_cloud_answers_are_cached_for_60_seconds(hub):
    scene(hub)
    items(hub, ADMIN)
    items(hub, USER)
    assert len(FakeBridge.task_calls) == 2  # one per printer
    hub.now += 61
    items(hub, ADMIN)
    assert len(FakeBridge.task_calls) == 4


def test_a_failing_printer_is_reported_and_the_rest_still_shows(hub):
    scene(hub)
    FakeBridge.task_fail = {SERIAL}
    st, body = hub.req("GET", "/api/history", user=ADMIN)
    assert st == 200 and [i["id"] for i in body["items"]] == [4] and body["unavailable"] == [SERIAL]
    FakeBridge.task_fail = set()
    hub.now += 61
    st, body = hub.req("GET", "/api/history", user=ADMIN)
    assert len(body["items"]) == 4 and body["unavailable"] == []


def test_needs_identity(hub):
    scene(hub)
    assert hub.req("GET", "/api/history")[0] == 401


def test_attribute_closest_start_wins_and_each_job_is_used_once():
    from history import attribute
    tasks = [task(1, "cube", 1000), task(2, "cube", 1100)]
    jobs = [{"id": "a", "file": "cube.gcode.3mf", "printer": SERIAL, "started_at": 1090}]
    assert {t: j["id"] for t, j in attribute(tasks, jobs).items()} == {2: "a"}
