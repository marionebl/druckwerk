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
    hub.owner("u-owner")
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


def test_finished_job_without_a_task_is_listed_as_job_only_and_queued_jobs_are_not(hub):
    scene(hub)
    job(hub, "ghost", "ghost.gcode.3mf", USER, 99999, state="failed")
    hub.app.queue.jobs.append({"id": "q1", "kind": "print", "name": "q", "file": "q.gcode.3mf", "target": SERIAL, "model": "", "state": "queued",
                               "owner_user_id": USER, "owner_name": "U", "created_at": 1, "filaments": [], "options": {}, "error": None})
    got = items(hub, ADMIN)
    ghost = next(i for i in got if i["job_id"] == "ghost")
    assert (ghost["id"], ghost["source"], ghost["status"], ghost["when"], ghost["printer_name"]) == ("job-ghost", "job", "failed", 99999, "Lab")
    assert ghost["job"]["state"] == "failed" and ghost["job"]["file"] == "ghost.gcode.3mf" and got[0] is ghost
    assert "q1" not in [i["job_id"] for i in got]
    assert [i["id"] for i in items(hub, USER)] == ["job-ghost", 1] and [i["id"] for i in items(hub, OTHER)] == [3]


def test_cancelled_job_that_never_reached_a_printer_still_shows_for_its_owner(hub):
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    FakeBridge.tasks = {SERIAL: []}
    j = job(hub, "c1", "x.gcode.3mf", USER, 5000, state="cancelled")
    j.pop("printer"), j.pop("started_at")
    (it,) = items(hub, USER)
    assert (it["id"], it["status"], it["started_at"], it["when"], it["serial"]) == ("job-c1", "cancelled", None, 5600, SERIAL)  # no start: the finish time orders it
    assert it["job"]["id"] == "c1"


def test_merged_item_carries_the_job_with_settings_and_slice(hub):
    scene(hub)
    j = hub.app.queue.history[0]
    j.update(options={"timelapse": True}, slice={"quality": "better", "process": "0.20mm"}, slice_job="abc", name="My cube", plate_png="api/queue/j1/plate.png")
    it = next(i for i in items(hub, ADMIN) if i["id"] == 1)
    assert it["job"]["options"] == {"timelapse": True} and it["job"]["slice"]["quality"] == "better" and it["job"]["name"] == "My cube"
    assert it["job"]["slice_job"] == "abc" and it["when"] == 5000
    assert next(i for i in items(hub, ADMIN) if i["id"] == 2)["job"] is None


def test_spool_prints_lists_settled_jobs_of_one_spool_with_their_task_ids(hub):
    scene(hub)
    by = {j["id"]: j for j in hub.app.queue.history}
    by["j1"]["spools_used"] = [{"spool_id": "77", "grams": 10.5}, {"spool_id": "78", "grams": 1}]
    by["j2"]["spools_used"] = [{"spool_id": "78", "grams": 2}]
    job(hub, "ghost", "ghost.gcode.3mf", USER, 99999, state="failed")["spools_used"] = [{"spool_id": "77", "grams": 3}]
    st, body = hub.req("GET", "/api/spools/77/prints", user=ADMIN)
    assert st == 200 and [(i["id"], i["spool_grams"]) for i in body["items"]] == [("job-ghost", 3), (1, 10.5)]
    assert [i["id"] for i in hub.req("GET", "/api/spools/78/prints", user=ADMIN)[1]["items"]] == [3, 1]
    assert [i["id"] for i in hub.req("GET", "/api/spools/77/prints", user=USER)[1]["items"]] == ["job-ghost", 1]
    assert hub.req("GET", "/api/spools/78/prints", user=OTHER)[1]["items"][0]["id"] == 3
    assert hub.req("GET", "/api/spools/79/prints", user=ADMIN)[1]["items"] == []
    assert hub.req("GET", "/api/spools/77/prints")[0] == 401


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
    # the jobs of the failing printer still show, without their cloud task
    assert st == 200 and [i["id"] for i in body["items"]] == ["job-j2", 4, "job-j1"] and body["unavailable"] == [SERIAL]
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


def test_a_print_nobody_started_here_names_the_printer_owner_or_keeps_the_old_text(hub):
    scene(hub)
    by = lambda: {i["id"]: i for i in items(hub, ADMIN)}
    assert by()[2]["printer_owner"] is None  # no owner: the UI keeps "Not started here"
    hub.ha.users.append({"id": "u-owner", "name": "Olga", "is_owner": False, "is_active": True, "system_generated": False,
                         "group_ids": ["system-users"]})
    hub.owner("u-owner")
    hub.now += 31  # the user list is cached for 30 s
    assert by()[2]["printer_owner"] == "Olga"
    assert "printer_owner" not in by()[1]  # a Druckwerk job says who started it
    assert "printer_owner" not in by()[4]
    hub.owner("")  # owner removed again
    assert by()[2]["printer_owner"] is None
