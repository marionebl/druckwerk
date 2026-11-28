"""Plates inbox: a fake GitHub API on a loopback port, the real hub app and queue."""
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pytest

from hubfakes import GCODE, SERIAL, SERIAL2, USER, make_3mf
from plates_inbox import MAX_BYTES, PlatesInbox, load_options

REPO, TOKEN = "fake-owner/fake-plates", "fake-token-123"


class FakeGitHub(BaseHTTPRequestHandler):
    tree = {}  # "plates/<id>/<name>" -> bytes
    sizes = {}  # path -> size to announce instead of the real one
    requests = []  # (method, path, authorization)
    delete_fail = False

    def reply(self, code, body, raw=False):
        data = body if raw else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @classmethod
    def tree_sha(cls, id):
        """Changes when a file of the folder is added, removed or changed, as GitHub's tree id does."""
        return hashlib.sha1(b"".join(hashlib.sha1(d).digest() + p.encode() for p, d in sorted(cls.tree.items()) if p.startswith("plates/%s/" % id))).hexdigest()

    def blob(self, sha):
        return next((d for d in self.tree.values() if hashlib.sha1(d).hexdigest() == sha), None)

    def do_GET(self):
        t = FakeGitHub
        t.requests.append(("GET", self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self.reply(401, {"message": "Bad credentials"})
        base = "/repos/%s/" % REPO
        path = unquote(self.path[len(base):])
        if path.startswith("git/blobs/"):
            data = self.blob(path.rsplit("/", 1)[1])
            return self.reply(200, data, raw=True) if data is not None else self.reply(404, {})
        path = path[len("contents/"):]
        if path == "plates":
            ids = sorted({p.split("/")[1] for p in t.tree})
            return self.reply(200, [{"type": "dir", "name": i, "path": "plates/" + i, "sha": t.tree_sha(i)} for i in ids]) if ids else self.reply(404, {})
        files = [(p, d) for p, d in sorted(t.tree.items()) if p.startswith(path + "/")]
        if not files:
            return self.reply(404, {})
        return self.reply(200, [{"type": "file", "name": p.rsplit("/", 1)[1], "path": p, "sha": hashlib.sha1(d).hexdigest(),
                                 "size": t.sizes.get(p, len(d))} for p, d in files])

    def do_DELETE(self):
        t = FakeGitHub
        t.requests.append(("DELETE", self.path, self.headers.get("Authorization")))
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        path = unquote(self.path[len("/repos/%s/contents/" % REPO):])
        if t.delete_fail:
            return self.reply(500, {"message": "boom"})
        if path not in t.tree or not body.get("sha") or not body.get("message"):
            return self.reply(422, {})
        del t.tree[path]
        self.reply(200, {"commit": {}})

    def log_message(self, *a):
        pass


@pytest.fixture
def gh(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    FakeGitHub.tree, FakeGitHub.sizes, FakeGitHub.requests, FakeGitHub.delete_fail = {}, {}, [], False
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()


def plate_bytes(tmp_path, gcode=GCODE):
    return open(make_3mf(tmp_path / "p.gcode.3mf", gcode=gcode), "rb").read()


def inbox(hub, gh, token=TOKEN):
    if not hub.app.registry.all():
        hub.setup_printers()
    return PlatesInbox(hub.app, REPO, token, hub.app.data_dir, api=gh)


def push(tmp_path, id="p1", name="cube.gcode.3mf", meta=None, data=None):
    FakeGitHub.tree["plates/%s/%s" % (id, name)] = data if data is not None else plate_bytes(tmp_path)
    if meta is not None:
        FakeGitHub.tree["plates/%s/job.json" % id] = json.dumps(meta).encode()


def test_new_folder_becomes_one_waiting_job_and_is_deleted(hub, gh, tmp_path):
    push(tmp_path)
    box = inbox(hub, gh)
    assert box.poll() == 1
    jobs = hub.app.queue.jobs
    assert len(jobs) == 1 and jobs[0]["state"] == "queued"
    assert jobs[0]["target"] == "any" and jobs[0]["model"] == "H2S" and jobs[0]["name"] == "cube"
    assert jobs[0]["owner_user_id"] is None and jobs[0]["owner_name"] == "Plates inbox"
    assert os.path.isfile(os.path.join(hub.s.SHARE, jobs[0]["file"])) and jobs[0]["grams"] == 75.22
    assert FakeGitHub.tree == {}  # one DELETE per file, the folder is gone
    assert [m for m, _, _ in FakeGitHub.requests if m == "DELETE"] == ["DELETE"]
    assert hub.sent() == []  # nothing started
    assert box.status()["last_error"] is None and box.status()["last_poll"]


def test_job_json_picks_printer_owner_and_name(hub, gh, tmp_path):
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    push(tmp_path, meta={"printer": SERIAL, "owner": "Ben", "name": "Bracket"})
    assert inbox(hub, gh).poll() == 1
    job = hub.app.queue.jobs[0]
    assert (job["target"], job["name"], job["owner_user_id"]) == (SERIAL, "Bracket", USER)


def test_unknown_or_ungranted_owner_falls_back_to_printer_owner_then_inbox(hub, gh, tmp_path):
    hub.setup_printers()
    hub.owner("u-other", SERIAL)
    push(tmp_path, "a", meta={"owner": "Ben"})  # Ben exists but has no grant
    push(tmp_path, "b", meta={"owner": "nobody"})
    assert inbox(hub, gh).poll() == 2
    assert [j["owner_user_id"] for j in hub.app.queue.jobs] == ["u-other", "u-other"]


def test_same_id_twice_gives_one_job_even_when_the_delete_failed(hub, gh, tmp_path):
    push(tmp_path)
    box = inbox(hub, gh)
    FakeGitHub.delete_fail = True
    assert box.poll() == 1
    assert "500" in box.status()["last_error"] and TOKEN not in box.status()["last_error"]
    assert len(FakeGitHub.tree) == 1
    assert box.poll() == 0  # still listed, taken: no second job
    FakeGitHub.delete_fail = False
    box2 = PlatesInbox(hub.app, REPO, TOKEN, hub.app.data_dir, api=gh)  # a restart keeps the taken ids
    assert box2.poll() == 0
    assert len(hub.app.queue.jobs) == 1 and FakeGitHub.tree == {}


def test_file_over_the_limit_is_skipped_without_download(hub, gh, tmp_path):
    push(tmp_path, "big")
    FakeGitHub.sizes["plates/big/cube.gcode.3mf"] = MAX_BYTES + 1
    push(tmp_path, "ok", name="ok.gcode.3mf")
    box = inbox(hub, gh)
    assert box.poll() == 1
    assert [j["name"] for j in hub.app.queue.jobs] == ["ok"]
    assert "plates/big/cube.gcode.3mf" in FakeGitHub.tree  # not deleted
    assert not any("git/blobs" in p for _, p, _ in FakeGitHub.requests if "big" in p)
    assert "too large" in box.status()["last_error"]
    n = len(FakeGitHub.requests)
    assert box.poll() == 0 and len(FakeGitHub.requests) == n + 1  # the folder is not listed again: its id is the same


@pytest.mark.parametrize("data,meta,text", [
    (b"not a zip", None, "not a sliced"),
    (None, {"printer": SERIAL2}, "P1S"),  # H2S file for the P1S: the model check
    (None, {"printer": "NOPE"}, "unknown printer"),
])
def test_bad_files_are_skipped_and_kept_in_the_repo(hub, gh, tmp_path, data, meta, text):
    push(tmp_path, data=data, meta=meta)
    box = inbox(hub, gh)
    assert box.poll() == 0 and hub.app.queue.jobs == []
    assert text in box.status()["last_error"]
    assert "plates/p1/cube.gcode.3mf" in FakeGitHub.tree


def test_gcode_guard_refuses_a_plate_that_prints_air(hub, gh, tmp_path):
    push(tmp_path, data=plate_bytes(tmp_path, GCODE.replace("M620 S0A\nT0\nM621 S0A\n", "")))
    box = inbox(hub, gh)
    assert box.poll() == 0 and hub.app.queue.jobs == []
    assert "AMS" in box.status()["last_error"]


def test_no_token_means_no_requests(hub, gh, tmp_path):
    push(tmp_path)
    box = inbox(hub, gh, token="")
    assert box.status()["enabled"] is False
    assert box.poll() == 0 and FakeGitHub.requests == [] and hub.app.queue.jobs == []


def test_github_down_or_wrong_token_is_an_error_not_a_crash(hub, gh, tmp_path):
    box = PlatesInbox(hub.app, REPO, "wrong", hub.app.data_dir, api=gh)
    assert box.poll() == 0 and "401" in box.status()["last_error"] and "wrong" not in box.status()["last_error"]
    box = PlatesInbox(hub.app, REPO, TOKEN, hub.app.data_dir, api="http://127.0.0.1:9")
    assert box.poll() == 0 and box.status()["last_error"]


def test_empty_repo_is_fine(hub, gh):
    box = inbox(hub, gh)
    assert box.poll() == 0 and box.status()["last_error"] is None


def test_options_come_from_options_json(tmp_path):
    assert load_options(str(tmp_path)) == {"repo": "marionebl/druckwerk-plates", "token": ""}
    (tmp_path / "options.json").write_text(json.dumps({"plates_repo": "a/b", "plates_token": "t"}))
    assert load_options(str(tmp_path)) == {"repo": "a/b", "token": "t"}


def test_setup_shows_inbox_status(hub, gh):
    hub.app.plates_inbox = inbox(hub, gh)
    st, body = hub.req("GET", "/api/setup", user="u-admin")
    assert body["plates_inbox"]["enabled"] is True and "token" not in json.dumps(body["plates_inbox"])


def test_a_rejected_folder_costs_no_call_until_its_commit_id_changes(hub, gh, tmp_path):
    push(tmp_path, data=b"not a zip", meta={"name": "x"})
    box = inbox(hub, gh)
    assert box.poll() == 0
    n = len(FakeGitHub.requests)
    assert box.poll() == 0 and box.poll() == 0
    assert [p for _, p, _ in FakeGitHub.requests[n:]].count("/repos/%s/contents/plates" % REPO) == 2
    assert len(FakeGitHub.requests) == n + 2  # the list of plates/ only: no folder listing, no job.json
    push(tmp_path, data=plate_bytes(tmp_path))  # the sender fixed the file: another id
    assert box.poll() == 1 and FakeGitHub.tree == {}


def test_a_name_with_a_space_does_not_break_the_delete(hub, gh, tmp_path):
    push(tmp_path)
    FakeGitHub.tree["plates/p1/read me.txt"] = b"hi"
    box = inbox(hub, gh)
    assert box.poll() == 1
    assert box.status()["last_error"] is None and FakeGitHub.tree == {}
