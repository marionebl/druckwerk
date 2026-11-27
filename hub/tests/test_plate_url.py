"""add_to_queue with file_url: the plate comes from the private plates repo on GitHub, checked by its SHA-256.

A local fake GitHub (http.server in a thread) stands in for api.github.com. The fake host reaches the app only through
App.plates_api, which tests set; a caller of the action cannot. The token below is a made-up test value.
"""
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

import actions
import plates
from hubfakes import SERIAL, USER, make_3mf

ACT = "X-Druckwerk-Act-As"
TOKEN = "ghp_FAKETESTTOKEN0123456789abcdefghij"
REPO = "marionebl/druckwerk-plates"
PATH = "plates/a1b2c3/benchy.gcode.3mf"


class FakeGitHub(BaseHTTPRequestHandler):
    files = {}  # contents path -> bytes
    seen = []  # (path with query, Authorization, Accept) of every request
    mode = None  # None, "redirect", "chunked"

    def log_message(self, *a):
        pass

    def do_GET(self):
        auth = self.headers.get("Authorization")
        FakeGitHub.seen.append((self.path, auth, self.headers.get("Accept")))
        u = urlsplit(self.path)
        if auth != "Bearer " + TOKEN:
            return self.reply(401, b'{"message":"Bad credentials"}')
        if FakeGitHub.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/blob")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        key = u.path.split("/contents/", 1)[1]
        if u.query != "ref=main" or key not in FakeGitHub.files:
            return self.reply(404, b'{"message":"Not Found"}')
        self.reply(200, FakeGitHub.files[key], length=FakeGitHub.mode != "chunked")

    def reply(self, status, body, length=True):
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        if length:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


@pytest.fixture
def gh(tmp_path):
    FakeGitHub.files, FakeGitHub.seen, FakeGitHub.mode = {}, [], None
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    src = make_3mf(tmp_path / "src.gcode.3mf")
    with open(src, "rb") as f:
        data = f.read()
    FakeGitHub.files[PATH] = data
    server.base = "http://127.0.0.1:%d" % server.server_address[1]
    server.data, server.sha = data, hashlib.sha256(data).hexdigest()
    server.url = "%s/repos/%s/contents/%s?ref=main" % (server.base, REPO, PATH)
    yield server
    server.shutdown()


@pytest.fixture
def hub(hub, gh):
    hub.setup_printers()
    hub.seed_share()
    hub.app.plates_api = gh.base
    set_options(hub, plates_token=TOKEN)
    return hub


def set_options(hub, **opts):
    os.makedirs(hub.app.data_dir, exist_ok=True)
    with open(os.path.join(hub.app.data_dir, "options.json"), "w") as f:
        json.dump(opts, f)


def act(hub, name, data, user=None):
    return hub.req("POST", "/api/actions/" + name, data, secret=True, headers={ACT: user} if user else None)


def add_url(hub, gh, url=None, sha=None, user=None, **extra):
    return act(hub, "add_to_queue", dict({"file_url": url or gh.url, "sha256": sha or gh.sha, "target": SERIAL}, **extra), user)


def stored(hub):
    return sorted(os.listdir(hub.app.media_dir))


def no_leak(hub, capsys, *answers):
    out = capsys.readouterr()
    for text in [out.out, out.err] + [json.dumps(a) for a in answers]:
        assert TOKEN not in text and "FAKETESTTOKEN" not in text and "Bearer" not in text


# ---- the good path
def test_a_plate_is_fetched_checked_and_queued(hub, gh, capsys):
    status, body = add_url(hub, gh)
    assert status == 200, body
    job = body["result"]["job"]
    assert job["target"] == SERIAL and job["state"] == "queued" and job["grams"] == 75.22
    assert job["name"] == "benchy" and os.path.isfile(os.path.join(hub.s.SHARE, job["file"]))
    assert stored(hub) == ["benchy.gcode.3mf"]  # the same folder PUT /api/upload uses
    with open(os.path.join(hub.app.media_dir, "benchy.gcode.3mf"), "rb") as f:
        assert f.read() == gh.data
    assert FakeGitHub.seen == [("/repos/%s/contents/%s?ref=main" % (REPO, PATH), "Bearer " + TOKEN, "application/vnd.github.raw")]
    assert hub.req("GET", "/api/files", secret=True)[1]["files"] == ["benchy.gcode.3mf"]
    no_leak(hub, capsys, body)


def test_the_tool_takes_the_same_fields_and_the_name_is_free_to_set(hub, gh):
    status, body = hub.req("POST", "/api/tools/druckwerk_add_to_queue", {"file_url": gh.url, "sha256": gh.sha.upper(), "name": "For Anna",
                                                                          "target": SERIAL}, secret=True)
    assert status == 200 and body["result"]["job"]["name"] == "For Anna"


def test_a_name_clash_gets_a_suffix_and_never_overwrites(hub, gh):
    first = add_url(hub, gh)[1]["result"]["job"]
    with open(os.path.join(hub.app.media_dir, "benchy.gcode.3mf"), "ab") as f:
        f.write(b"\0")  # a change that an overwrite would undo
    second = add_url(hub, gh)[1]["result"]["job"]
    names = stored(hub)
    assert len(names) == 2 and "benchy.gcode.3mf" in names and first["id"] != second["id"]
    other = [n for n in names if n != "benchy.gcode.3mf"][0]
    assert other.startswith("benchy_") and other.endswith(".gcode.3mf")
    with open(os.path.join(hub.app.media_dir, "benchy.gcode.3mf"), "rb") as f:
        assert f.read() == gh.data + b"\0"


def test_a_chunked_answer_without_content_length_works(hub, gh):
    FakeGitHub.mode = "chunked"
    assert add_url(hub, gh)[0] == 200


def test_the_name_is_sanitized():
    assert plates.safe_name("Benächy+1 (v2).gcode.3mf") == "Ben_chy_1 _v2_.gcode.3mf"
    assert plates.safe_name("..hidden.gcode.3mf") == "hidden.gcode.3mf"
    assert plates.safe_name("-x.gcode.3mf") == "p-x.gcode.3mf"
    assert "/" not in plates.safe_name("a b.gcode.3mf") and len(plates.safe_name("x" * 500 + ".gcode.3mf")) <= 118 + 10


# ---- refusals
def test_a_wrong_sha256_stores_nothing(hub, gh, capsys):
    status, body = add_url(hub, gh, sha="0" * 64)
    assert status == 400 and "SHA-256 mismatch" in body["error"] and gh.sha in body["error"]
    assert stored(hub) == [] and hub.app.queue.jobs == []  # not even a .part file
    no_leak(hub, capsys, body)


@pytest.mark.parametrize("sha", ["abc", "g" * 64, "0" * 63, "0" * 65, ""])
def test_a_bad_sha256_is_refused_before_any_request(hub, gh, sha):
    status, body = act(hub, "add_to_queue", {"file_url": gh.url, "sha256": sha, "target": SERIAL})
    assert status == 400 and "sha256" in body["error"] and FakeGitHub.seen == []


@pytest.mark.parametrize("url", [
    "https://evil.example/repos/marionebl/druckwerk-plates/contents/plates/a/b.gcode.3mf?ref=main",
    "https://github.com/marionebl/druckwerk-plates/raw/main/plates/a/b.gcode.3mf",
    "https://raw.githubusercontent.com/marionebl/druckwerk-plates/main/plates/a/b.gcode.3mf",
    "https://api.github.com.evil.example/repos/marionebl/druckwerk-plates/contents/plates/a/b.gcode.3mf?ref=main",
    "ftp://api.github.com/repos/marionebl/druckwerk-plates/contents/plates/a/b.gcode.3mf?ref=main",
    "file:///etc/passwd",
])
def test_a_url_on_another_host_or_scheme_is_never_fetched(hub, gh, capsys, url):
    status, body = add_url(hub, gh, url=url)
    assert status == 400 and "file_url must be" in body["error"] and FakeGitHub.seen == []
    no_leak(hub, capsys, body)


@pytest.mark.parametrize("make", [
    lambda gh: gh.url.replace("http://", "http://user:pw@"),  # userinfo
    lambda gh: gh.url.replace("http://", "http://api.github.com@"),
    lambda gh: gh.url.replace("127.0.0.1:%d" % gh.server_address[1], "127.0.0.1:%d" % (gh.server_address[1] + 1)),  # other port
    lambda gh: gh.url.replace("127.0.0.1:%d" % gh.server_address[1], "127.0.0.1"),  # no port
    lambda gh: gh.url.replace("http://", "https://"),  # other scheme
    lambda gh: gh.url + "#frag",
])
def test_userinfo_ports_and_schemes_are_refused(hub, gh, make):
    status, body = add_url(hub, gh, url=make(gh))
    assert status == 400 and FakeGitHub.seen == []


@pytest.mark.parametrize("repo", ["marionebl/druckwerk", "someone/druckwerk-plates", "marionebl/druckwerk-plates2"])
def test_a_url_of_another_repo_is_never_fetched(hub, gh, repo):
    url = "%s/repos/%s/contents/%s?ref=main" % (gh.base, repo, PATH)
    status, body = add_url(hub, gh, url=url)
    assert status == 400 and "druckwerk-plates" in body["error"] and FakeGitHub.seen == []


def test_the_option_plates_repo_sets_the_allowed_repo(hub, gh):
    set_options(hub, plates_token=TOKEN, plates_repo="someone/other")
    assert add_url(hub, gh)[0] == 400 and FakeGitHub.seen == []  # the default repo is no longer allowed
    url = "%s/repos/someone/other/contents/%s?ref=main" % (gh.base, PATH)
    assert add_url(hub, gh, url=url)[0] == 200 and len(FakeGitHub.seen) == 1  # the option's repo is allowed now
    set_options(hub, plates_token=TOKEN, plates_repo="not a repo")
    status, body = add_url(hub, gh)
    assert status == 400 and "plates_repo" in body["error"]


@pytest.mark.parametrize("tail", [
    "plates/../../x/b.gcode.3mf?ref=main", "plates/%2e%2e/b.gcode.3mf?ref=main", "plates//b.gcode.3mf?ref=main",
    "plates/a/b.gcode.3mf", "plates/a/b.gcode.3mf?ref=main&x=1", "plates/a/b.gcode.3mf?x=main", "plates/a/b.gcode.3mf?ref=",
    "plates/a/b.gcode.3mf?ref=../x", "plates/a%2f..%2fb.gcode.3mf?ref=main",
])
def test_a_bad_path_or_query_is_refused(hub, gh, tail):
    url = "%s/repos/%s/contents/%s" % (gh.base, REPO, tail)
    status, body = add_url(hub, gh, url=url)
    assert status == 400 and FakeGitHub.seen == []


@pytest.mark.parametrize("tail", ["plates/a/benchy.stl", "plates/a/benchy.3mf", "plates/a/benchy.gcode", "plates/a/x.gcode.3mf.txt", "plates/a/"])
def test_only_gcode_3mf_names_are_accepted(hub, gh, tail):
    status, body = add_url(hub, gh, url="%s/repos/%s/contents/%s?ref=main" % (gh.base, REPO, tail))
    assert status == 400 and FakeGitHub.seen == []


def test_a_missing_token_gets_a_clear_error_and_no_request(hub, gh, capsys):
    for opts in ({}, {"plates_token": ""}, {"plates_token": "   "}, {"plates_repo": REPO}):
        set_options(hub, **opts)
        status, body = add_url(hub, gh)
        assert status == 400 and "plates_token" in body["error"] and "app options" in body["error"]
    os.remove(os.path.join(hub.app.data_dir, "options.json"))  # no options file at all
    status, body = add_url(hub, gh)
    assert status == 400 and "plates_token" in body["error"] and FakeGitHub.seen == []


def test_a_token_with_odd_characters_is_refused_without_echo(hub, gh, capsys):
    set_options(hub, plates_token="abc\r\nX-Evil: 1")
    status, body = add_url(hub, gh)
    assert status == 400 and "abc" not in body["error"] and FakeGitHub.seen == []


def test_github_errors_do_not_show_the_token(hub, gh, capsys):
    set_options(hub, plates_token="ghp_WRONGTOKEN")
    status, body = add_url(hub, gh)
    assert status == 502 and "refused the token" in body["error"] and "WRONGTOKEN" not in body["error"]
    set_options(hub, plates_token=TOKEN)
    status, body = add_url(hub, gh, url="%s/repos/%s/contents/plates/zz/none.gcode.3mf?ref=main" % (gh.base, REPO))
    assert status == 502 and "404" in body["error"]
    no_leak(hub, capsys, body)
    assert stored(hub) == []


def test_a_redirect_is_not_followed(hub, gh, capsys):
    FakeGitHub.mode = "redirect"
    status, body = add_url(hub, gh)
    assert status == 502 and "redirect" in body["error"] and len(FakeGitHub.seen) == 1
    no_leak(hub, capsys, body)


def test_an_unreachable_server_gives_a_short_error(hub, gh, capsys):
    hub.app.plates_api = "http://127.0.0.1:1"
    url = "http://127.0.0.1:1/repos/%s/contents/%s?ref=main" % (REPO, PATH)
    status, body = add_url(hub, gh, url=url)
    assert status == 502 and "could not be reached" in body["error"]
    no_leak(hub, capsys, body)


def test_an_oversize_plate_is_refused_and_removed(hub, gh, monkeypatch):
    monkeypatch.setattr(plates, "MAX_BYTES", len(gh.data) - 1)
    status, body = add_url(hub, gh)  # Content-Length says it
    assert status == 413 and stored(hub) == []
    FakeGitHub.mode = "chunked"  # no Content-Length: the streaming count says it
    status, body = add_url(hub, gh)
    assert status == 413 and stored(hub) == [] and hub.app.queue.jobs == []


def test_the_size_cap_is_64_mb_and_the_time_cap_120_s():
    assert plates.MAX_BYTES == 64 * 1024 * 1024 and plates.TOTAL_TIMEOUT == 120


def test_a_slow_download_stops_at_the_deadline(hub, gh):
    ticks = iter(range(0, 10000, 100))  # each check of the clock is 100 s later
    with pytest.raises(plates.PlateError) as e:
        plates.fetch(gh.url, gh.sha, hub.app.media_dir, REPO, TOKEN, api_base=gh.base, clock=lambda: next(ticks))
    assert e.value.status == 504 and stored(hub) == []


def test_a_file_that_is_no_sliced_3mf_is_refused(hub, gh):
    for data in (b"not a zip", ):
        FakeGitHub.files[PATH] = data
        status, body = add_url(hub, gh, sha=hashlib.sha256(data).hexdigest())
        assert status == 400 and "3MF" in body["error"] and stored(hub) == []
    import zipfile
    path = hub.tmp / "plain.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", "x")
    FakeGitHub.files[PATH] = path.read_bytes()
    status, body = add_url(hub, gh, sha=hashlib.sha256(path.read_bytes()).hexdigest())
    assert status == 400 and "no sliced plate" in body["error"] and stored(hub) == []


def test_a_refused_add_leaves_nothing_in_the_media_folder(hub, gh):
    status, body = add_url(hub, gh, user=USER)  # no grant
    assert status == 403 and body["error"].startswith("rule:")
    assert stored(hub) == [] and hub.app.queue.jobs == []
    hub.grant(USER, SERIAL)
    assert add_url(hub, gh, user=USER)[0] == 200 and stored(hub) == ["benchy.gcode.3mf"]


# ---- the fields
@pytest.mark.parametrize("data, text", [
    ({"file": "a.gcode.3mf", "file_url": "https://x", "sha256": "0" * 64}, "either file or file_url"),
    ({}, "Give file"),
    ({"target": SERIAL}, "Give file"),
    ({"file_url": "https://x"}, "sha256 is required with file_url"),
    ({"file": "a.gcode.3mf", "sha256": "0" * 64}, "sha256 goes with file_url only"),
])
def test_exactly_one_of_file_or_file_url(hub, data, text):
    for call in (lambda d: act(hub, "add_to_queue", d), lambda d: hub.req("POST", "/api/tools/druckwerk_add_to_queue", d, secret=True)):
        status, body = call(data)
        assert status == 400 and text in body["error"]
    assert FakeGitHub.seen == [] and hub.app.queue.jobs == []


def test_a_caller_cannot_set_the_host(hub, gh):
    for key in ("api_base", "plates_api", "host", "token", "plates_token"):
        status, body = act(hub, "add_to_queue", {"file_url": gh.url, "sha256": gh.sha, key: "http://evil.example"})
        assert status == 400 and "extra keys not allowed" in body["error"]
    assert FakeGitHub.seen == []


def test_the_old_file_path_still_works_without_the_new_options(hub):
    os.remove(os.path.join(hub.app.data_dir, "options.json"))
    status, body = act(hub, "add_to_queue", {"file": "a.gcode.3mf", "target": SERIAL})
    assert status == 200 and body["result"]["job"]["file"] and FakeGitHub.seen == []
    set_options(hub)  # an options.json from an older version: {}
    assert act(hub, "add_to_queue", {"file": "b.gcode.3mf", "target": SERIAL})[0] == 200
    with open(os.path.join(hub.app.data_dir, "options.json"), "w") as f:
        f.write("not json")
    assert act(hub, "add_to_queue", {"file": "c.gcode.3mf", "target": SERIAL})[0] == 200


def test_the_manifest_describes_the_new_fields(hub):
    got = hub.req("GET", "/api/integration/manifest", secret=True)[1]
    action = next(a for a in got["actions"] if a["name"] == "add_to_queue")
    assert action["fields"]["file"]["required"] is False and "file_url" in action["fields"] and "sha256" in action["fields"]
    tool = next(t for t in got["llm"]["tools"] if t["name"] == "druckwerk_add_to_queue")
    assert tool["parameters"]["required"] == [] and {"file", "file_url", "sha256"} <= set(tool["parameters"]["properties"])
    text = tool["description"] + json.dumps(tool["parameters"])
    assert "plates repo" in text and "SHA-256" in text and "contents API" in text
    assert TOKEN not in json.dumps(got)


def test_the_options_in_config_yaml():
    import yaml
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["options"] == {"plates_repo": REPO, "plates_token": ""}
    assert cfg["schema"] == {"plates_repo": "str?", "plates_token": "password?"}
    assert actions.ACTIONS["add_to_queue"]["fields"]["file_url"]["kind"] == "string"
