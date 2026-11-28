"""Bounds for disk and memory: pruned files, size-checked zip members, checked downloads."""
import http.client
import os
import threading
import tracemalloc
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import app as app_module
import files
import preview
from app import HttpError, fetch_file, fetch_image
from hubfakes import ADMIN, SERIAL, FakeBridge
from test_history_settings import FIXTURE, scene

MB = 1 << 20
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20


# -- CORE-06: cloud files --

def put_cloud(hub, n, size=10):
    d = os.path.join(hub.app.data_dir, "cloud")
    os.makedirs(d, exist_ok=True)
    for i in range(n):
        p = os.path.join(d, "%d.gcode.3mf" % (100 + i))
        with open(p, "wb") as f:
            f.write(b"x" * size)
        os.utime(p, (1000 + i, 1000 + i))
    return d


def fetch_cloud(hub, monkeypatch, tid="8"):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    hub.setup_printers()
    FakeBridge.files = {"/dl/f.gcode.3mf": open(FIXTURE, "rb").read()}
    FakeBridge.file_url = "http://127.0.0.1:%d/dl/f.gcode.3mf" % hub.bridge.server_address[1]
    return hub.app.cloud_file({"id": tid, "source": "cloud", "serial": SERIAL, "model_id": "M1", "profile_id": 55})


def test_cloud_files_stay_at_20_and_the_new_one_stays(hub, monkeypatch):
    d = put_cloud(hub, 25)
    path = fetch_cloud(hub, monkeypatch)
    names = sorted(os.listdir(d))
    assert len(names) == 20 and os.path.basename(path) in names, names
    assert "100.gcode.3mf" not in names and "124.gcode.3mf" in names  # the oldest went first


def test_cloud_files_stay_under_the_size_limit(hub, monkeypatch):
    monkeypatch.setattr(app_module, "CLOUD_MAX_BYTES", 10000, raising=False)
    d = put_cloud(hub, 4, size=2000)
    path = fetch_cloud(hub, monkeypatch)
    left = os.listdir(d)
    assert os.path.basename(path) in left and sum(os.path.getsize(os.path.join(d, n)) for n in left) <= 10000, left


# -- QUEUE-13: files of a job that leaves the history --

def with_files(hub, job):
    """The share copy exists (queue_job made it); add the plate picture and the saved settings."""
    app = hub.app
    for d, name in (("plates", job["id"] + ".png"), ("settings", job["id"] + ".json")):
        os.makedirs(os.path.join(app.data_dir, d), exist_ok=True)
        with open(os.path.join(app.data_dir, d, name), "wb") as f:
            f.write(b"x")
    return [os.path.join(hub.s.SHARE, job["file"]), os.path.join(app.data_dir, "plates", job["id"] + ".png"),
            os.path.join(app.data_dir, "settings", job["id"] + ".json")]


def test_a_cancelled_job_keeps_its_files_and_can_be_retried(hub):
    hub.setup_printers()
    job = hub.queue_job(file="a")
    paths = with_files(hub, job)
    assert hub.req("POST", "/api/queue/%s/cancel" % job["id"], {}, user=ADMIN)[0] == 200
    assert [os.path.exists(p) for p in paths] == [True] * 3  # the files go when the job leaves the history
    assert hub.req("POST", "/api/queue/%s/retry" % job["id"], {}, user=ADMIN)[0] == 200


def test_a_failed_job_keeps_its_share_copy(hub):
    hub.setup_printers()
    job = hub.queue_job(file="a")
    paths = with_files(hub, job)
    q = hub.app.queue
    q._finish(q.jobs[0], "failed", "boom")
    q._save()
    assert [os.path.exists(p) for p in paths] == [True] * 3  # retry needs the file


def test_a_job_that_leaves_the_history_leaves_no_file(hub, monkeypatch):
    import queue_engine
    monkeypatch.setattr(queue_engine, "HISTORY", 1)
    hub.setup_printers()
    first, second = hub.queue_job(file="a"), hub.queue_job(file="b")
    paths = with_files(hub, first)
    q = hub.app.queue
    for j in list(q.jobs):
        q._finish(j, "failed", "boom")
    q._save()
    assert [os.path.exists(p) for p in paths] == [False] * 3
    assert os.path.exists(os.path.join(hub.s.SHARE, second["file"]))


def test_the_file_a_printer_reports_as_its_last_file_stays(hub, monkeypatch):
    import queue_engine
    monkeypatch.setattr(queue_engine, "HISTORY", 1)
    hub.setup_printers()
    first, second = hub.queue_job(file="a"), hub.queue_job(file="b")
    share = with_files(hub, first)[0]
    hub.app.sender.results[SERIAL] = {"last_path": share}
    for job in (first, second):
        assert hub.req("POST", "/api/queue/%s/cancel" % job["id"], {}, user=ADMIN)[0] == 200
    assert [j["id"] for j in hub.app.queue.history] == [second["id"]] and os.path.exists(share)


def test_retry_of_a_cancelled_job_without_its_file_is_refused(hub):
    hub.setup_printers()
    job = hub.queue_job(file="a")
    hub.req("POST", "/api/queue/%s/cancel" % job["id"], {}, user=ADMIN)
    os.remove(os.path.join(hub.s.SHARE, job["file"]))
    st, body = hub.req("POST", "/api/queue/%s/retry" % job["id"], {}, user=ADMIN)
    assert st == 409 and "file" in body["error"], (st, body)
    assert [j["id"] for j in hub.app.queue.history] == [job["id"]]


# -- CORE-09: zip members --

def bomb(path, member, size):
    """A zip of a few hundred kB whose member claims `size` bytes."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z, z.open(member, "w") as out:
        for _ in range(size // MB):
            out.write(b"\0" * MB)
    return str(path)


def peak(fn):
    tracemalloc.start()
    try:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            err = e
        else:
            err = None
        return tracemalloc.get_traced_memory()[1], err
    finally:
        tracemalloc.stop()


def test_read_member_refuses_a_big_member_before_it_reads(tmp_path):
    p = bomb(tmp_path / "b.zip", "Metadata/slice_info.config", 60 * MB)
    with zipfile.ZipFile(p) as z:
        used, err = peak(lambda: files.read_member(z, "Metadata/slice_info.config", MB))
    assert isinstance(err, ValueError) and used < 10 * MB, (err, used)


def test_a_sliced_file_with_a_huge_config_is_refused_without_memory_growth(hub, tmp_path):
    p = bomb(tmp_path / "x.gcode.3mf", "Metadata/slice_info.config", 60 * MB)
    with zipfile.ZipFile(p, "a") as z:
        z.writestr("Metadata/plate_1.gcode", "G1 X1\n")
    used, err = peak(lambda: hub.app.check_sliced(p))
    assert isinstance(err, ValueError) and used < 10 * MB, (err, used)


def test_the_gcode_download_refuses_a_big_member_without_memory_growth(hub, tmp_path):
    scene(hub)
    hub.s.jobs["a1b2c3d4e5f6"]["result"] = bomb(tmp_path / "big.gcode.3mf", "Metadata/plate_1.gcode", 160 * MB)
    out = {}

    def get():
        out["r"] = hub.req("GET", "/api/history/7/gcode", user="u-user")
    used, _ = peak(get)
    assert out["r"][0] == 404 and used < 10 * MB, (out["r"][0], used)


def stream(hub, path, user):
    """Read a response in chunks and drop them, so the test itself holds no copy."""
    c = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=30)
    c.request("GET", path, headers={"X-Remote-User-Id": user, "X-Remote-User-Name": user})
    r, n = c.getresponse(), 0
    while chunk := r.read(MB):
        n += len(chunk)
    c.close()
    return r.status, n


def test_the_history_downloads_do_not_hold_the_file_in_memory(hub, tmp_path):
    scene(hub)
    with open(hub.s.jobs["a1b2c3d4e5f6"]["model"], "wb") as f:
        f.write(b"\0" * (30 * MB))
    out = {}
    used, _ = peak(lambda: out.update(file=stream(hub, "/api/history/7/file", "u-user")))
    assert out["file"] == (200, 30 * MB) and used < 10 * MB, (out, used)
    hub.s.jobs["a1b2c3d4e5f6"]["result"] = bomb(tmp_path / "g.gcode.3mf", "Metadata/plate_1.gcode", 100 * MB)
    used, _ = peak(lambda: out.update(gcode=stream(hub, "/api/history/7/gcode", "u-user")))
    assert out["gcode"] == (200, 100 * MB) and used < 10 * MB, (out, used)


def test_plate_png_does_not_load_a_gcode_without_a_picture(tmp_path):
    p = tmp_path / "p.gcode.3mf"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z, z.open("Metadata/plate_1.gcode", "w") as out:
        for _ in range(100):
            out.write(b"; comment line\n" * (MB // 15))
    used, err = peak(lambda: preview.plate_png(str(p)))
    assert err is None and used < 10 * MB, (err, used)


def test_plate_png_refuses_a_gcode_over_the_limit(tmp_path):
    p = bomb(tmp_path / "p.gcode.3mf", "Metadata/plate_1.gcode", 160 * MB)
    used, err = peak(lambda: preview.plate_png(p))
    assert err is None and used < 10 * MB, (err, used)


# -- CORE-08: downloads --

class Remote(BaseHTTPRequestHandler):
    def do_GET(self):
        how = self.path.strip("/")
        if how == "redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if how == "short":  # fewer bytes than announced
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(PNG)
            self.wfile.flush()
            self.close_connection = True
            return
        if how == "bad":  # not an HTTP answer at all
            self.wfile.write(b"garbage\r\n\r\n")
            self.close_connection = True
            return
        body, ctype = (PNG, "image/png") if how == "ok" else (b"<svg onload=alert(1)/>", "image/svg+xml") if how == "svg" else (b"<html>", "text/html")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Remote)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield lambda p: "http://127.0.0.1:%d/%s" % (srv.server_address[1], p)
    srv.shutdown()


def test_a_good_image_and_file_still_download(remote, tmp_path):
    assert fetch_image(remote("ok")) == (PNG, "image/png")
    fetch_file(remote("ok"), str(tmp_path / "f"))
    assert open(tmp_path / "f", "rb").read() == PNG


@pytest.mark.parametrize("how", ["short", "bad", "redirect", "svg", "html"])
def test_a_bad_image_answer_is_a_502(remote, how):
    with pytest.raises(HttpError) as e:
        fetch_image(remote(how))
    assert e.value.status == 502


@pytest.mark.parametrize("how", ["short", "bad", "redirect"])
def test_a_bad_file_answer_is_a_502_and_leaves_nothing(remote, tmp_path, how):
    dest = tmp_path / "cloud" / "f"
    with pytest.raises(HttpError) as e:
        fetch_file(remote(how), str(dest))
    assert e.value.status == 502 and os.listdir(tmp_path / "cloud") == []
