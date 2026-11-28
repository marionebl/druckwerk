"""Files on the SD card: a torn or empty file must not stop the app, a write must reach the disk, a cloud file has one writer."""
import glob
import os
import shutil
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import queue_registry
from filament import LoadRequests, Prices
from hubfakes import ADMIN, HERE, SERIAL, FakeBridge, Hub
from notify import Notifier
from nozzles import NozzleRequests
from queue_access import Grants
from queue_engine import Queue
from queue_registry import Registry, write_json
from sender import Sender

FIXTURE = os.path.join(HERE, "fixtures", "settings.gcode.3mf")


def corrupt(d, name, text=""):
    with open(os.path.join(d, name), "w") as f:
        f.write(text)


@pytest.fixture
def hub_at(tmp_path):
    hubs = []

    def start():
        h = Hub(tmp_path)
        hubs.append(h)
        return h
    yield start
    for h in hubs:
        h.server.shutdown()
        h.bridge.shutdown()


LOADERS = {  # file -> function that builds the object that reads it
    "printers.json": lambda d: Registry(d),
    "removed_printers.json": lambda d: Registry(d),
    "queue.json": lambda d: Queue(Registry(d), d, lambda e: None, lambda *a: None, lambda s: []),
    "grants.json": lambda d: Grants(Registry(d), d, lambda e: None, time.time),
    "sender.json": lambda d: Sender(types.SimpleNamespace(data_dir=d)),
    "integration.json": lambda d: Notifier(types.SimpleNamespace(data_dir=d)),
    "spool_prices.json": lambda d: Prices(d),
    "load_requests.json": lambda d: LoadRequests(d),
    "nozzle_requests.json": lambda d: NozzleRequests(d),
}


@pytest.mark.parametrize("name", sorted(LOADERS))
def test_an_empty_file_is_set_aside_and_the_loader_starts_empty(tmp_path, name):
    d = str(tmp_path)
    corrupt(d, name)
    LOADERS[name](d)
    assert glob.glob(os.path.join(d, name + ".corrupt-*")), os.listdir(d)
    assert not os.path.exists(os.path.join(d, name))


def test_the_app_starts_with_an_empty_queue_json(tmp_path, hub_at):
    os.makedirs(tmp_path / "data")
    corrupt(str(tmp_path / "data"), "queue.json")
    hub = hub_at()
    assert hub.req("GET", "/api/queue", user=ADMIN)[0] == 200
    assert hub.app.queue.jobs == [] and glob.glob(str(tmp_path / "data" / "queue.json.corrupt-*"))


def test_write_json_leaves_no_temp_file_after_an_error(tmp_path):
    with pytest.raises(TypeError):
        write_json(str(tmp_path / "x.json"), {"a": object()})
    assert os.listdir(tmp_path) == []


def test_write_json_syncs_before_the_rename(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(queue_registry.os, "fsync", lambda fd: order.append("fsync"), raising=False)
    real = os.replace
    monkeypatch.setattr(queue_registry.os, "replace", lambda a, b: (order.append("replace"), real(a, b)))
    write_json(str(tmp_path / "x.json"), {"a": 1})
    assert order == ["fsync", "replace"]


def test_a_torn_saved_settings_file_gives_the_next_source(tmp_path, hub_at):
    hub = hub_at()
    hub.setup_printers()
    jid = hub.sliced(owner=ADMIN)
    shutil.copyfile(FIXTURE, hub.s.jobs[jid]["result"])
    hub.app.queue.history.append({
        "id": "j1", "file": "cube.gcode.3mf", "source": "cube.gcode.3mf", "target": SERIAL, "model": "", "filaments": [], "error": None,
        "options": {}, "state": "done", "owner_user_id": ADMIN, "owner_name": "A", "created_at": 8990, "printer": SERIAL,
        "started_at": 8995, "finished_at": 9600, "name": "Cube", "slice_job": jid})
    hub.s.jobs.pop(jid)  # the slice is gone: only the saved file is left
    FakeBridge.tasks = {SERIAL: []}
    os.makedirs(tmp_path / "data" / "settings", exist_ok=True)
    corrupt(str(tmp_path / "data" / "settings"), "j1.json", '{"plate": {"pre')
    st, d = hub.req("GET", "/api/history/job-j1/settings", user=ADMIN)
    assert st == 200 and d["source"] != "saved", (st, d)


def test_save_settings_and_save_job_go_through_write_json(tmp_path, hub_at, monkeypatch):
    import app as app_mod
    hub = hub_at()
    calls, real = [], queue_registry.write_json
    for mod in (app_mod, hub.s):
        monkeypatch.setattr(mod, "write_json", lambda p, d: (calls.append(p), real(p, d)))
    hub.app.save_settings("j9", FIXTURE)
    hub.s.JOBS = str(tmp_path / "jobs")
    os.makedirs(os.path.join(hub.s.JOBS, "s1"))
    hub.s.save_job({"id": "s1", "state": "done"})
    assert [os.path.basename(c) for c in calls] == ["j9.json", "job.json"], calls



def test_job_json_stays_readable_for_other_users(tmp_path, hub_at):
    hub = hub_at()
    hub.s.JOBS = str(tmp_path / "jobs")
    os.makedirs(os.path.join(hub.s.JOBS, "s1"))
    hub.s.save_job({"id": "s1", "state": "done"})
    # browser_test.sh reads it as the runner user while the image writes it as root
    assert os.stat(os.path.join(hub.s.JOBS, "s1", "job.json")).st_mode & 0o777 == 0o644

class Slow(BaseHTTPRequestHandler):
    data = open(FIXTURE, "rb").read()

    def log_message(self, *a):
        pass

    def do_GET(self):
        half = len(self.data) // 2
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.data)))
        self.end_headers()
        self.wfile.write(self.data[:half])
        self.wfile.flush()
        time.sleep(0.5)
        self.wfile.write(self.data[half:])


def test_two_requests_for_one_cloud_file_both_get_it(tmp_path, hub_at, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    hub = hub_at()
    dl = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=dl.serve_forever, daemon=True).start()
    try:
        hub.setup_printers()
        FakeBridge.file_url = "http://127.0.0.1:%d/f" % dl.server_address[1]
        it = {"id": "8", "source": "cloud", "serial": SERIAL, "model_id": "M1", "profile_id": 55}
        out = []

        def get():
            try:
                out.append(os.path.basename(hub.app.cloud_file(it)))
            except Exception as e:  # noqa: BLE001
                out.append("error: %s" % e)
        ts = [threading.Thread(target=get) for _ in range(2)]
        [t.start() for t in ts]
        [t.join(20) for t in ts]
        assert out == ["8.gcode.3mf"] * 2 and hub.app.cloud_miss == {}, (out, hub.app.cloud_miss)
        assert os.listdir(tmp_path / "data" / "cloud") == ["8.gcode.3mf"]
    finally:
        dl.shutdown()
