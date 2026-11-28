"""End-to-end tests: server.py as a subprocess against the mock cloud and mock daemon.
SERVER_PY overrides the file under test (used to prove the tests can fail)."""
import base64
import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mock_cloud  # noqa: E402
import mock_daemon  # noqa: E402

MOCK = "%s -I %s" % (sys.executable, os.path.join(HERE, "mock_daemon.py"))
SERVER_PY = os.environ.get("SERVER_PY", os.path.join(HERE, "..", "server.py"))


def read(path):
    with open(path) as f:
        return f.read()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_range(n=4):
    """A port whose next n-1 neighbours are free too, since daemons take consecutive ports."""
    while True:
        base, socks = free_port(), []
        try:
            for p in range(base, base + n):
                socks.append(socket.socket())
                socks[-1].bind(("127.0.0.1", p))
            return base
        except OSError:
            pass
        finally:
            for k in socks:
                k.close()


class Server:
    """One server.py process with its own data dir and log file."""

    def __init__(self, cloud, daemon_cmd=MOCK, setup=None, extra_env=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "data")
        self.cert = os.path.join(self.tmp.name, "slicer_base64.cer")
        with open(self.cert, "w") as f:
            f.write("CERT")
        if setup:
            setup(self.data)
        self.port = free_port()
        self.base = free_range()  # first daemon port; the next ones are base+1, ...
        self.logpath = os.path.join(self.tmp.name, "server.log")
        self.bodies = []  # every HTTP response body we received
        env = dict(os.environ, DW_DATA=self.data, DW_PORT=str(self.port), DW_HOST="127.0.0.1",
                   DW_API_BASE="http://127.0.0.1:%d" % cloud.server_port, DW_DAEMON_PORT_BASE=str(self.base),
                   DW_CERT=self.cert, DW_DAEMON_CMD=daemon_cmd, DW_BACKOFF_MIN="0.2", DW_BACKOFF_MAX="0.8",
                   **(extra_env or {}))
        self.log = open(self.logpath, "w")
        self.proc = subprocess.Popen([sys.executable, "-I", SERVER_PY], env=env, stdout=self.log, stderr=self.log,
                                     start_new_session=True)  # so close() can kill the daemons too
        for _ in range(50):
            try:
                self.req("GET", "/api/health")
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def req(self, method, path, body=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.request(method, path, raw if raw is not None else (json.dumps(body) if body is not None else None))
        r = c.getresponse()
        data = r.read()
        c.close()
        self.bodies.append(data)
        return r.status, data

    def json(self, method, path, body=None):
        status, data = self.req(method, path, body)
        return status, json.loads(data)

    def acct(self, id, name=""):
        return os.path.join(self.data, "accounts", id, name)

    def daemon(self, port, path):
        """Ask a mock daemon directly, not through the server."""
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", path)
        data = json.loads(c.getresponse().read())
        c.close()
        return data

    def logtext(self):
        self.log.flush()
        with open(self.logpath) as f:
            return f.read()

    def close(self):
        os.killpg(self.proc.pid, signal.SIGKILL)  # the server and the daemons it started
        self.proc.wait(10)
        self.log.close()
        self.tmp.cleanup()


def wait_for(fn, seconds=10):
    for _ in range(int(seconds * 10)):
        try:
            value = fn()
            if value:
                return value
        except OSError:
            pass
        time.sleep(0.1)
    return fn()


def daemon_up(s, port):
    return wait_for(lambda: s.daemon(port, "/health"))


def login(s, email, code=True):
    st, d = s.json("POST", "/api/login/start", {"email": email, "password": "pw"})
    if code and d.get("state") == "need_code":
        st, d = s.json("POST", "/api/login/code", {"code": "123456", "email": email})
    return d


def latest_tokens(cloud):
    return [t for _, t, r in cloud.issued] + [r for _, t, r in cloud.issued]


class FlowTest(unittest.TestCase):
    """Tests share one server and run in order: login first."""

    @classmethod
    def setUpClass(cls):
        cls.cloud = mock_cloud.start()
        cls.s = Server(cls.cloud)

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_01_not_logged_in_at_start(self):
        self.assertEqual(self.s.json("GET", "/api/health")[1], {"server": "ok", "plugin": {"state": "ready"}, "accounts": []})  # ADAPTED

    def test_02_login_with_code_writes_credentials(self):
        st, d = self.s.json("POST", "/api/login/start", {"email": "a@example.com", "password": "pw"})
        self.assertEqual((st, d), (200, {"state": "need_code", "account": "a_example_com"}))  # ADAPTED
        self.assertIn("/v1/user-service/user/sendemail/code", [c[0] for c in self.cloud.calls])
        st, d = self.s.json("POST", "/api/login/code", {"code": "000000"})
        self.assertNotEqual(st, 200)  # a wrong code does not log in
        st, d = self.s.json("POST", "/api/login/code", {"code": "123456"})
        self.assertEqual((st, d), (200, {"state": "done", "account": "a_example_com"}))  # ADAPTED
        with open(self.s.acct("a_example_com", "credentials.toml"), "rb") as f:
            t = tomllib.load(f)
        token, refresh = self.cloud.tokens["a@example.com"]
        self.assertEqual(t["cloud"], {"token": token, "refresh_token": refresh, "uid": "4242", "email": "a@example.com"})
        self.assertEqual([p["serial"] for p in t["printers"].values()], ["SERIAL001", "SERIAL002"])
        self.assertEqual(len(t["printers"]), 2)  # same name twice still gives two sections
        for name in ("credentials.toml", "printers.json"):
            self.assertEqual(stat.S_IMODE(os.stat(self.s.acct("a_example_com", name)).st_mode), 0o600, name)
        self.assertEqual({c[2] for c in self.cloud.calls}, {"OrcaSlicer"})

    def test_03_health_logged_in_and_daemon_started(self):
        daemon_up(self.s, self.s.base)
        _, h = self.s.json("GET", "/api/health")
        (a,) = h["accounts"]  # ADAPTED
        self.assertEqual((a["id"], a["email"], a["logged_in"]), ("a_example_com", "a@example.com", True))
        self.assertTrue(a["daemon"]["running"])
        self.assertEqual(a["daemon"]["health"]["status"], "ok")
        self.assertEqual(a["daemon"]["health"]["credentials"], self.s.acct("a_example_com", "credentials.toml"))

    def test_04_printers_merged(self):
        _, p = self.s.json("GET", "/api/printers")
        self.assertEqual([x["serial"] for x in p], ["SERIAL001", "SERIAL002"])
        self.assertEqual(p[0]["dev_product_name"], "P1S")
        self.assertEqual((p[0]["account"], p[0]["email"]), ("a_example_com", "a@example.com"))  # ADAPTED
        self.assertEqual(p[0]["live"]["gcode_state"], "IDLE")
        self.assertIsNone(p[1]["live"])
        self.assertNotIn("ACCESS-1234", json.dumps(p))

    def test_05_status_and_ams_proxy(self):
        st, a = self.s.json("GET", "/api/ams/SERIAL001")
        self.assertEqual(st, 200)
        self.assertEqual(a["ams"], mock_daemon.AMS["ams"])  # ADAPTED: the mock adds served_by
        _, s = self.s.json("GET", "/api/status/SERIAL001")
        self.assertEqual(s["print"]["nozzle_temper"], 21.5)
        self.assertEqual(self.s.req("GET", "/api/ams/NOPE")[0], 404)

    def test_05b_camera_protocols_pass_through_and_mint_timeout_keeps_daemon(self):
        _, u = self.s.json("GET", "/api/camera/SERIAL001/url?protocols=agora")
        self.assertEqual(u["path"], "/camera/SERIAL001/url?protocols=agora")
        _, u = self.s.json("GET", "/api/camera/SERIAL001/url?protocols=x%22y")
        self.assertEqual(u["path"], "/camera/SERIAL001/url")  # bad value dropped
        pid = lambda: self.s.json("GET", "/api/health")[1]["accounts"][0]["daemon"]["health"]["pid"]
        before = pid()
        st, body = self.s.json("GET", "/api/camera/SERIAL001/snapshot")
        self.assertEqual(st, 502)
        self.assertIn("camera URL callback", body["error"])
        time.sleep(1)
        self.assertEqual(pid(), before)  # the status is fresh, so the daemon stays

    def test_05c_light_and_rfid_commands(self):
        st, _ = self.s.json("POST", "/api/light/SERIAL001", {"on": True})
        self.assertEqual(st, 200)
        st, _ = self.s.json("POST", "/api/rfid/SERIAL001", {"ams_id": 1, "slot_id": 2})
        self.assertEqual(st, 200)
        cmds = self.s.daemon(self.s.base, "/commands")
        self.assertEqual([c["system"]["led_node"] for c in cmds[:2]], ["chamber_light", "chamber_light2"])
        self.assertEqual(cmds[0]["system"]["led_mode"], "on")
        self.assertEqual(cmds[2]["print"], {"command": "gcode_line", "sequence_id": "0", "param": "M620 R6 \n"})
        self.assertEqual(self.s.json("POST", "/api/light/SERIAL001", {"on": "yes"})[0], 400)
        self.assertEqual(self.s.json("POST", "/api/rfid/SERIAL001", {"ams_id": 0, "slot_id": 4})[0], 400)
        self.assertEqual(self.s.json("POST", "/api/light/NOPE", {"on": True})[0], 404)
        self.assertEqual(len(self.s.daemon(self.s.base, "/commands")), 3)  # rejected requests sent nothing

    def test_05d_pause_command(self):
        before = len(self.s.daemon(self.s.base, "/commands"))
        st, _ = self.s.json("POST", "/api/pause/SERIAL001", {})
        self.assertEqual(st, 200)
        cmds = self.s.daemon(self.s.base, "/commands")
        # The payload the vendor slicer's command_task_pause and the vendor HA integration's PAUSE send.
        self.assertEqual(cmds[before:], [{"print": {"command": "pause", "sequence_id": "0", "param": ""}}])
        self.assertEqual(self.s.json("POST", "/api/pause/NOPE", {})[0], 404)
        self.assertEqual(len(self.s.daemon(self.s.base, "/commands")), before + 1)

    def sent_params(self, **options):
        body = {"serial": "SERIAL001", "filename": "opt.gcode.3mf", "file_b64": base64.b64encode(b"x").decode()}
        st, d = self.s.json("POST", "/api/print", dict(body, **options))
        return st, (self.s.daemon(self.s.base, "/prints")[-1]["params"] if st == 200 else d)

    def test_05e_print_options_reach_the_daemon(self):
        # Defaults send exactly what the bridge sent before the options existed.
        st, got = self.sent_params()
        self.assertEqual(got, {"device_id": "SERIAL001", "filename": "opt.gcode.3mf", "project_name": "opt",
                               "bed_leveling": True, "flow_cali": True, "vibration_cali": True, "timelapse": False,
                               "use_ams": True})
        # "auto" is bool false plus the vendor slicer's integer 2; nozzle offset has no bool, only the integer.
        st, got = self.sent_params(bed_leveling="auto", flow_cali=False, nozzle_offset_cali="auto", vibration_cali=False,
                                   layer_inspect=True, bed_type="textured_plate", ext_change_assist=True,
                                   timelapse=True, use_ams=False)
        self.assertEqual(got, {
            "device_id": "SERIAL001", "filename": "opt.gcode.3mf", "project_name": "opt",
            "bed_leveling": False, "auto_bed_leveling": 2, "flow_cali": False, "auto_offset_cali": 2,
            "vibration_cali": False, "layer_inspect": True, "bed_type": "textured_plate", "ext_change_assist": True,
            "timelapse": True, "use_ams": False})
        st, got = self.sent_params(flow_cali="auto", nozzle_offset_cali=True, bed_leveling=False)
        self.assertEqual((got["flow_cali"], got["auto_flow_cali"], got["auto_offset_cali"], got["bed_leveling"]),
                         (False, 2, 1, False))
        self.assertNotIn("auto_bed_leveling", got)

    def test_05f_print_options_are_validated(self):
        n = len(self.s.daemon(self.s.base, "/prints"))
        for bad in ({"bed_leveling": "maybe"}, {"flow_cali": 2}, {"bed_type": "gold_plate"}, {"layer_inspect": "yes"},
                    {"vibration_cali": "auto"}, {"ext_change_assist": 1}, {"nozzle_offset_cali": None}):
            st, d = self.sent_params(**bad)
            self.assertEqual(st, 400, bad)
            self.assertIn(list(bad)[0], d["error"])
        self.assertEqual(len(self.s.daemon(self.s.base, "/prints")), n)  # nothing reached the daemon

    def test_05g_drying_status_lists_units_with_a_dryer(self):
        st, d = self.s.json("GET", "/api/drying/SERIAL001")
        self.assertEqual(st, 200)
        self.assertEqual(d, [
            {"ams_id": 0, "name": "AMS 2 Pro", "status": "drying", "active": True, "left_min": 90, "filament": "PLA",
             "temp": 45, "hours": 12, "temp_min": 45, "temp_max": 65},
            {"ams_id": 128, "name": "AMS HT", "status": "off", "active": False, "left_min": 0, "filament": "",
             "temp": None, "hours": None, "temp_min": 45, "temp_max": 85}])
        self.assertEqual(self.s.json("GET", "/api/drying/SERIAL002")[1], [])  # fun2 bit 5 off: no remote drying
        self.assertEqual(self.s.req("GET", "/api/drying/NOPE")[0], 404)

    def test_05h_drying_start_and_stop_payloads(self):
        before = len(self.s.daemon(self.s.base, "/commands"))
        st, _ = self.s.json("POST", "/api/ams/SERIAL001/drying", {"ams_id": 0, "temp": 55, "hours": 8, "filament": "PLA"})
        self.assertEqual(st, 200)
        st, _ = self.s.json("POST", "/api/ams/SERIAL001/drying", {"ams_id": 128, "temp": 80, "hours": 12, "filament": "PA",
                                                                  "rotate_tray": True, "cooling_temp": 60})
        self.assertEqual(st, 200)
        st, _ = self.s.json("POST", "/api/ams/SERIAL001/drying/stop", {"ams_id": 0})
        self.assertEqual(st, 200)
        # The payloads of the vendor slicer's DevDryer::CtrlStartDrying and CtrlStopDrying.
        self.assertEqual(self.s.daemon(self.s.base, "/commands")[before:], [
            {"print": {"command": "ams_filament_drying", "sequence_id": "0", "ams_id": 0, "zone_id": 0, "mode": 1,
                       "filament": "PLA", "temp": 55, "duration": 8, "humidity": 0, "rotate_tray": False,
                       "cooling_temp": 50, "close_power_conflict": False}},
            {"print": {"command": "ams_filament_drying", "sequence_id": "0", "ams_id": 128, "zone_id": 0, "mode": 1,
                       "filament": "PA", "temp": 80, "duration": 12, "humidity": 0, "rotate_tray": True,
                       "cooling_temp": 60, "close_power_conflict": False}},
            {"print": {"command": "ams_filament_drying", "sequence_id": "0", "ams_id": 0, "mode": 0, "filament": "",
                       "temp": 0, "duration": 0, "humidity": 0, "rotate_tray": False, "cooling_temp": 0,
                       "close_power_conflict": False}}])

    def test_05i_drying_rejects_bad_input(self):
        n = len(self.s.daemon(self.s.base, "/commands"))
        post = lambda path, body, serial="SERIAL001": self.s.json("POST", "/api/ams/%s/%s" % (serial, path), body)[0]
        good = {"ams_id": 0, "temp": 50, "hours": 6, "filament": "PLA"}
        self.assertEqual(post("drying", dict(good, temp=44)), 400)  # below the dryer minimum
        self.assertEqual(post("drying", dict(good, temp=66, filament="PETG")), 400)  # above the AMS 2 Pro maximum (65)
        self.assertEqual(post("drying", dict(good, ams_id=128, temp=86)), 400)  # above the AMS HT maximum (85)
        self.assertEqual(post("drying", dict(good, temp=60)), 400)  # above the PLA limit (55)
        self.assertEqual(post("drying", dict(good, hours=0)), 400)
        self.assertEqual(post("drying", dict(good, hours=73)), 400)
        self.assertEqual(post("drying", dict(good, hours=1.5)), 400)
        self.assertEqual(post("drying", dict(good, temp="50")), 400)
        self.assertEqual(post("drying", dict(good, ams_id=1)), 400)  # a plain AMS has no dryer
        self.assertEqual(post("drying", dict(good, ams_id=7)), 400)
        self.assertEqual(post("drying", dict(good, rotate_tray="yes")), 400)
        self.assertEqual(post("drying", {}), 400)
        self.assertEqual(post("drying", good, "SERIAL002"), 400)  # printer without remote drying
        self.assertEqual(post("drying", good, "NOPE"), 404)
        self.assertEqual(post("drying/stop", {"ams_id": 300}), 400)
        self.assertEqual(len(self.s.daemon(self.s.base, "/commands")), n)  # nothing was sent

    def test_06_print_delivers_exact_bytes(self):
        blob = bytes(range(256)) * 40 + b"\r\n--boundary\r\n\x00\xff" + os.urandom(2000)
        st, d = self.s.json("POST", "/api/print", {"serial": "SERIAL001", "filename": "cube.gcode.3mf",
                                                   "file_b64": base64.b64encode(blob).decode(), "timelapse": True})
        self.assertEqual(st, 200)
        self.assertEqual(d["result"], "sent")
        self.assertEqual(d["return_code"], -1)
        got = self.s.daemon(self.s.base, "/prints")[-1]  # ADAPTED: read from the daemon process
        self.assertEqual(base64.b64decode(got["file_b64"]), blob)
        self.assertEqual(got["params"], {
            "device_id": "SERIAL001", "filename": "cube.gcode.3mf", "project_name": "cube", "bed_leveling": True,
            "flow_cali": True, "vibration_cali": True, "timelapse": True, "use_ams": True})

    def test_06b_print_progress_follows_the_daemon_log(self):
        import threading
        body = {"serial": "SERIAL001", "filename": "progress.gcode.3mf", "file_b64": base64.b64encode(b"x").decode()}
        t = threading.Thread(target=lambda: self.s.json("POST", "/api/print", body))
        self.assertEqual(self.s.json("GET", "/api/print-progress/SERIAL001")[1], {})  # nothing is being sent
        t.start()
        try:
            end = time.time() + 5
            while time.time() < end:
                st, d = self.s.json("GET", "/api/print-progress/SERIAL001")
                if d.get("percent") == 45:
                    break
                time.sleep(0.1)
            self.assertEqual(d, {"stage": "upload", "percent": 45})
            self.assertEqual(self.s.json("GET", "/api/print-progress/SERIAL002")[1], {})  # another printer
        finally:
            t.join()
        self.assertEqual(self.s.json("GET", "/api/print-progress/SERIAL001")[1], {})  # done: cleared

    def test_07_print_error_status_passes_through(self):
        st, d = self.s.json("POST", "/api/print", {"serial": "SERIAL001", "filename": "fail.gcode.3mf",
                                                   "file_b64": base64.b64encode(b"x").decode()})
        self.assertEqual((st, d["result"]), (502, "error"))

    def test_08_print_rejects_bad_input_and_big_bodies(self):
        self.assertEqual(self.s.req("POST", "/api/print", {"serial": "SERIAL001", "filename": "a.3mf",
                                                           "file_b64": "!!notbase64"})[0], 400)
        c = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=10)
        c.putrequest("POST", "/api/print")
        c.putheader("Content-Length", str(50 * 1024 * 1024 + 1))
        c.endheaders()  # headers only: the server must answer without waiting for the body
        r = c.getresponse()
        self.assertEqual(r.status, 413)
        self.s.bodies.append(r.read())
        c.close()

    def test_09_page_uses_relative_urls(self):
        st, page = self.s.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertNotIn(b'fetch("/', page)
        self.assertIn(b'"api/health"', page)
        self.assertIn(b'"api/accounts/"', page)

    def test_99_token_never_leaks(self):
        for secret in latest_tokens(self.cloud):
            for body in self.s.bodies:
                self.assertNotIn(secret.encode(), body)
            self.assertNotIn(secret, self.s.logtext())
        self.assertGreater(len(self.s.bodies), 8)


class FlowVariantsTest(unittest.TestCase):
    def test_tfa_and_direct_login(self):
        cloud = mock_cloud.start()
        s = Server(cloud)
        try:
            self.assertEqual(s.json("POST", "/api/login/start", {"email": "tfa@x.org", "password": "pw"})[1],
                             {"state": "need_tfa", "account": "tfa_x_org"})  # ADAPTED
            self.assertEqual(s.json("POST", "/api/login/code", {"code": "654321"})[1],
                             {"state": "done", "account": "tfa_x_org"})  # ADAPTED
            self.assertEqual(s.json("POST", "/api/login/start", {"email": "direct@x.org", "password": "pw"})[1],
                             {"state": "done", "account": "direct_x_org"})  # ADAPTED
            self.assertEqual(s.req("POST", "/api/login/start", {"email": "a@x.org"})[0], 400)
        finally:
            s.close()


class StatusTest(unittest.TestCase):
    """A daemon that answers /status slowly must not turn into 503 for the hub."""

    def test_slow_daemon_gets_the_last_good_status_and_calls_are_shared(self):
        cloud = mock_cloud.start()
        log, slow = (tempfile.NamedTemporaryFile(delete=False).name for _ in range(2))
        os.unlink(slow)
        s = Server(cloud, extra_env={"MOCK_STATUS_LOG": log, "MOCK_STATUS_SLOW": slow, "DW_STATUS_CACHE": "0.5",
                                     "DW_STATUS_TIMEOUT": "1"})
        try:
            login(s, "a@example.com")
            daemon_up(s, s.base)
            c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=30)
            c.request("GET", "/api/status/SERIAL001")
            r = c.getresponse()
            good = r.read()
            self.assertEqual(r.status, 200)
            calls = read(log).count("status")
            c.request("GET", "/api/status/SERIAL001")  # inside the cache time: no daemon call
            r = c.getresponse()
            self.assertEqual((r.status, r.read()), (200, good))
            self.assertEqual(read(log).count("status"), calls)  # the cache hit
            time.sleep(0.7)
            with open(slow, "w") as f:
                f.write("3")  # over the 1 s timeout
            results = []

            def get():
                cc = http.client.HTTPConnection("127.0.0.1", s.port, timeout=30)
                cc.request("GET", "/api/status/SERIAL001")
                rr = cc.getresponse()
                results.append((rr.status, rr.getheader("X-Status-Age"), rr.read()))
            threads = [threading.Thread(target=get) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual([r[0] for r in results], [200] * 5)
            self.assertTrue(all(float(r[1]) >= 0.5 and r[2] == good for r in results), results)
            self.assertEqual(read(log).count("status") - calls, 1)  # five requests, one daemon call
            # without any good answer the answer is still 503
            st, _ = s.req("GET", "/api/status/SERIAL002")
            self.assertEqual(st, 503)
        finally:
            s.close()
            for f in (log, slow):
                if os.path.exists(f):
                    os.unlink(f)


class SupervisorTest(unittest.TestCase):
    def test_restarts_exiting_daemon_and_masks_token_in_its_output(self):
        cloud = mock_cloud.start()
        marker = tempfile.NamedTemporaryFile(delete=False).name
        # A "daemon" that records each start, prints the credentials (token included), then fails.
        script = 'echo started >> %s; echo daemon-says-hi; cat "$1"; exit 1' % marker  # ADAPTED: path is an argument
        s = Server(cloud, "sh -c '%s' sh" % script)
        try:
            time.sleep(0.6)
            self.assertEqual(read(marker), "")  # not started before login
            s.json("POST", "/api/login/start", {"email": "direct@x.org", "password": "pw"})
            for _ in range(60):
                if read(marker).count("started") >= 3:
                    break
                time.sleep(0.1)
            self.assertGreaterEqual(read(marker).count("started"), 3)
            self.assertGreaterEqual(s.json("GET", "/api/health")[1]["accounts"][0]["daemon"]["restarts"], 2)  # ADAPTED
            log = s.logtext()
            self.assertIn("daemon-says-hi", log)
            self.assertIn("restarting in", log)
            self.assertIn("uid = ", log)  # the daemon output reached the log ...
            for secret in latest_tokens(cloud):
                self.assertNotIn(secret, log)  # ... with the token masked
        finally:
            s.close()
            os.unlink(marker)

    def test_fatal_line_kills_a_hanging_daemon(self):
        cloud = mock_cloud.start()
        marker = tempfile.NamedTemporaryFile(delete=False).name
        s = Server(cloud, "sh -c 'echo started >> %s; echo \"terminate called x\"; exec sleep 600' sh" % marker)
        try:
            s.json("POST", "/api/login/start", {"email": "direct@x.org", "password": "pw"})
            self.assertTrue(wait_for(lambda: read(marker).count("started") >= 2))  # killed and restarted
        finally:
            s.close()
            os.unlink(marker)


class MultiAccountTest(unittest.TestCase):
    """Two accounts on one server, in order."""

    @classmethod
    def setUpClass(cls):
        cls.cloud = mock_cloud.start()
        cls.s = Server(cls.cloud)

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_01_two_logins_two_daemons_on_two_ports(self):
        s = self.s
        self.assertEqual(login(s, "a@example.com"), {"state": "done", "account": "a_example_com"})
        self.assertEqual(login(s, "b@example.com"), {"state": "done", "account": "b_example_com"})
        a, b = daemon_up(s, s.base), daemon_up(s, s.base + 1)
        for acc, h, port in (("a_example_com", a, s.base), ("b_example_com", b, s.base + 1)):
            self.assertEqual(h["credentials"], s.acct(acc, "credentials.toml"))
            self.assertEqual(h["home"], s.acct(acc, "home"))
            with open(s.acct(acc, "port")) as f:
                self.assertEqual(int(f.read()), port)
            with open(os.path.join(h["home"], ".local/share/bambox/cert/slicer_base64.cer")) as f:
                self.assertEqual(f.read(), "CERT")
        self.assertNotEqual(a["pid"], b["pid"])
        self.assertNotEqual(a["home"], b["home"])
        _, h = s.json("GET", "/api/health")
        self.assertEqual([x["id"] for x in h["accounts"]], ["a_example_com", "b_example_com"])
        self.assertTrue(all(x["daemon"]["running"] and x["daemon"]["health"] for x in h["accounts"]))

    def test_02_routing_by_serial(self):
        s = self.s
        _, p = s.json("GET", "/api/printers")
        self.assertEqual([(x["serial"], x["account"], x["email"]) for x in p], [
            ("SERIAL001", "a_example_com", "a@example.com"), ("SERIAL002", "a_example_com", "a@example.com"),
            ("SERIALB01", "b_example_com", "b@example.com")])
        self.assertEqual(p[2]["live"]["serial"], "SERIALB01")
        self.assertNotIn("access_code", json.dumps(p))
        creds_b = s.acct("b_example_com", "credentials.toml")
        self.assertEqual(s.json("GET", "/api/status/SERIALB01")[1]["served_by"], creds_b)
        self.assertEqual(s.json("GET", "/api/ams/SERIALB01")[1]["served_by"], creds_b)
        self.assertEqual(s.json("GET", "/api/status/SERIAL001")[1]["served_by"], s.acct("a_example_com", "credentials.toml"))
        blob = os.urandom(3000) + b"\r\n--x\r\n\x00"
        st, d = s.json("POST", "/api/print", {"serial": "SERIALB01", "filename": "b.gcode.3mf",
                                              "file_b64": base64.b64encode(blob).decode()})
        self.assertEqual((st, d["device_id"]), (200, "SERIALB01"))
        (got,) = s.daemon(s.base + 1, "/prints")
        self.assertEqual(base64.b64decode(got["file_b64"]), blob)
        self.assertEqual(s.daemon(s.base, "/prints"), [])  # account A's daemon saw nothing
        self.assertEqual(s.json("POST", "/api/rfid/SERIALB01", {"ams_id": 0, "slot_id": 3})[0], 200)
        (cmd,) = s.daemon(s.base + 1, "/commands")
        self.assertEqual(cmd["print"], {"command": "ams_get_rfid", "sequence_id": "0", "ams_id": 0, "slot_id": 3, "mode": 0})
        self.assertEqual(s.req("GET", "/api/status/NOPE")[0], 404)
        self.assertEqual(s.req("POST", "/api/print", {"serial": "NOPE", "filename": "a.3mf", "file_b64": "eA=="})[0], 404)

    def test_02b_tasks_use_the_token_of_the_printers_account(self):
        s = self.s
        st, a = s.json("GET", "/api/tasks?serial=SERIAL001&limit=2")
        self.assertEqual(st, 200)
        self.assertEqual([t["id"] for t in a], [103, 102])  # limit reaches the cloud and newest comes first
        self.assertEqual(a[1], {"id": 102, "serial": "SERIAL001", "title": "cube-aexamplecom", "status": "failed", "status_code": "failed",
                                "started_at": 1795773600, "ended_at": 1795775400, "seconds": 1800, "grams": 4.5,
                                "cover_url": "https://cdn.example/c.png", "plate": 2, "design_title": "Cube"})
        self.assertEqual(a[0]["title"], "Vase-aexamplecom")  # the design title stands in for an empty title
        self.assertIsNone(a[0]["ended_at"])
        b = s.json("GET", "/api/tasks?serial=SERIALB01")[1]
        self.assertEqual(b[1]["title"], "cube-bexamplecom")
        used = {q: e for q, e in self.cloud.task_calls}
        self.assertEqual(used["/v1/user-service/my/tasks?deviceId=SERIAL001&limit=2"], "a@example.com")
        self.assertEqual(used["/v1/user-service/my/tasks?deviceId=SERIALB01&limit=50"], "b@example.com")
        self.assertEqual((b[2]["status"], b[2]["status_code"]), ("finished", 2))

    def test_02c_tasks_errors(self):
        s = self.s
        self.assertEqual(s.req("GET", "/api/tasks?serial=NOPE")[0], 404)
        self.assertEqual(s.req("GET", "/api/tasks")[0], 400)
        for bad in ("0", "101", "x"):
            self.assertEqual(s.req("GET", "/api/tasks?serial=SERIAL001&limit=" + bad)[0], 400, bad)
        st, d = s.json("GET", "/api/tasks?serial=SERIAL002")  # the cloud answers 500
        self.assertEqual(st, 502)
        self.assertIn("HTTP 500", d["error"])
        known = dict(self.cloud.owner)
        self.cloud.owner.clear()  # the cloud no longer knows any token: 401
        st, d = s.json("GET", "/api/tasks?serial=SERIAL001")
        self.cloud.owner.update(known)
        self.assertEqual(st, 502)
        self.assertIn("HTTP 401", d["error"])

    def test_02d_filament_library_of_one_account(self):
        s = self.s
        st, a = s.json("GET", "/api/filament?account=a_example_com")
        self.assertEqual(st, 200)
        self.assertEqual(len(a), 60)  # two pages
        self.assertEqual([q.split("&")[0].split("?")[1] for q, _ in self.cloud.filament_calls[-2:]], ["offset=0", "offset=50"])
        self.assertEqual(a[0], {"id": "9000", "brand": "Acme", "type": "PLA", "name": "PLA Basic aexamplecom", "colour": "FF0000",
                                "grams_left": 500, "grams_total": 1000, "printer": None, "unit": None, "slot": None,
                                "in_printer": False, "rfid": None, "account": "a_example_com"})
        self.assertEqual((a[1]["printer"], a[1]["unit"], a[1]["slot"], a[1]["in_printer"], a[1]["rfid"], a[1]["grams_left"]),
                         ("SERIAL001", 0, 2, True, "RF1", None))
        b = s.json("GET", "/api/filament?account=b_example_com")[1]
        self.assertIn("PLA Basic bexamplecom", b[0]["name"])
        self.assertEqual({e for _, e in self.cloud.filament_calls}, {"a@example.com", "b@example.com"})
        for shape in ("array", "data"):  # the other list shapes the vendor slicer accepts
            self.cloud.list_shape = shape
            self.assertEqual(len(s.json("GET", "/api/filament?account=a_example_com")[1]), 60, shape)
        self.cloud.list_shape = "hits"

    def test_02e_filament_write_routes(self):
        s = self.s
        st, new = s.json("POST", "/api/filament?account=a_example_com",
                         {"brand": "Prusa", "type": "PETG", "name": "PETG Blue", "colour": "1f6feb", "grams_left": 800, "grams_total": 1000})
        self.assertEqual(st, 200)
        self.assertEqual((new["id"], new["brand"], new["colour"], new["grams_left"]), ("9160", "Prusa", "1F6FEB", 800))
        sent = next(b for p, b, *_ in reversed(self.cloud.calls) if p == mock_cloud.FILAMENT and "filamentName" in b)
        self.assertEqual(sent, {"filamentVendor": "Prusa", "filamentType": "PETG", "filamentName": "PETG Blue", "color": "1F6FEBFF",
                                "colors": ["1F6FEBFF"], "colorType": 2, "netWeight": 800, "totalNetWeight": 1000, "rolls": 1,
                                "createType": "manual", "isSupport": False})
        st, upd = s.json("PUT", "/api/filament/9160?account=a_example_com", {"grams_left": 750})
        self.assertEqual((st, upd["grams_left"], upd["brand"]), (200, 750, "Prusa"))
        put = next(c for c in reversed(self.cloud.calls) if c[-1] == "PUT")
        self.assertEqual((put[0], put[1]), (mock_cloud.FILAMENT, {"id": 9160, "netWeight": 750}))  # the id travels in the body
        self.assertEqual(s.json("PUT", "/api/filament/1?account=a_example_com", {"grams_left": 1})[0], 502)  # the cloud knows no id 1
        self.assertEqual(s.req("PUT", "/api/filament/9160?account=a_example_com", {"grams_left": -1})[0], 400)
        self.assertEqual(s.req("POST", "/api/filament?account=a_example_com", {"type": "PLA"})[0], 400)  # a name is required
        self.assertEqual(s.json("DELETE", "/api/filament/9160?account=a_example_com")[0], 200)
        dele = next(c for c in reversed(self.cloud.calls) if c[-1] == "DELETE")
        self.assertEqual((dele[0], dele[1]), (mock_cloud.FILAMENT + "/batch", {"ids": [9160]}))
        self.assertEqual(len(s.json("GET", "/api/filament?account=a_example_com")[1]), 60)

    def test_02f_filament_errors(self):
        s = self.s
        self.assertEqual(s.req("GET", "/api/filament?account=nope")[0], 404)
        self.assertEqual(s.req("GET", "/api/filament")[0], 404)  # no account named
        known = dict(self.cloud.owner)
        self.cloud.owner.clear()  # the cloud no longer knows the token: 401
        st, d = s.json("GET", "/api/filament?account=a_example_com")
        self.cloud.owner.update(known)
        self.assertEqual(st, 502)
        self.assertIn("HTTP 401", d["error"])
        self.assertNotIn("TOKEN-SECRET", d["error"])

    def test_03_relogin_replaces_token(self):
        s = self.s
        old = self.cloud.tokens["b@example.com"]
        pid = daemon_up(s, s.base + 1)["pid"]
        self.assertEqual(login(s, "b@example.com")["state"], "done")
        new = self.cloud.tokens["b@example.com"]
        self.assertNotEqual(old, new)
        with open(s.acct("b_example_com", "credentials.toml")) as f:
            text = f.read()
        self.assertIn(new[0], text)
        self.assertNotIn(old[0], text)
        self.assertEqual(len(s.json("GET", "/api/health")[1]["accounts"]), 2)  # replaced, not added
        self.assertTrue(wait_for(lambda: daemon_up(s, s.base + 1)["pid"] != pid))  # daemon restarted on the same port

    def test_04_delete_stops_daemon_and_removes_dir(self):
        s = self.s
        self.assertEqual(s.json("DELETE", "/api/accounts/b_example_com")[1], {"deleted": "b_example_com"})
        self.assertFalse(os.path.exists(s.acct("b_example_com")))
        with self.assertRaises(OSError):
            s.daemon(s.base + 1, "/health")
        self.assertEqual([x["id"] for x in s.json("GET", "/api/health")[1]["accounts"]], ["a_example_com"])
        self.assertEqual(s.req("GET", "/api/status/SERIALB01")[0], 404)
        self.assertEqual(s.req("DELETE", "/api/accounts/b_example_com")[0], 404)
        self.assertTrue(daemon_up(s, s.base))  # account A is untouched
        time.sleep(1.0)  # the supervisor must not bring the deleted daemon back
        with self.assertRaises(OSError):
            s.daemon(s.base + 1, "/health")

    def test_05_login_code_needs_email_when_two_are_pending(self):
        s = self.s
        s.json("POST", "/api/login/start", {"email": "c@example.com", "password": "pw"})
        s.json("POST", "/api/login/start", {"email": "d@example.com", "password": "pw"})
        self.assertEqual(s.req("POST", "/api/login/code", {"code": "123456"})[0], 400)
        self.assertEqual(s.json("POST", "/api/login/code", {"code": "123456", "email": "d@example.com"})[1],
                         {"state": "done", "account": "d_example_com"})

    def test_99_no_token_in_responses_or_log(self):
        for secret in latest_tokens(self.cloud):
            for body in self.s.bodies:
                self.assertNotIn(secret.encode(), body)
            self.assertNotIn(secret, self.s.logtext())


class TwoDaemonsLogTest(unittest.TestCase):
    def test_token_masked_in_log_output_of_both_daemons(self):
        cloud = mock_cloud.start()
        s = Server(cloud, "sh -c 'echo up; cat \"$1\"; sleep 600' sh")
        try:
            login(s, "a@example.com")
            login(s, "b@example.com")
            self.assertTrue(wait_for(lambda: s.logtext().count("uid = ") >= 2))
            log = s.logtext()
            self.assertIn("[a_example_com] daemon: ", log)
            self.assertIn("[b_example_com] daemon: ", log)
            for secret in latest_tokens(cloud):
                self.assertNotIn(secret, log)
        finally:
            s.close()


class LiveTest(unittest.TestCase):
    """The live camera route against the mock daemon's streams (SERIAL001 H.264, SERIAL002 JPEG)."""
    IDLE = 2  # seconds the add-on keeps an unused upstream open

    @classmethod
    def setUpClass(cls):
        cls.cloud = mock_cloud.start()
        cls.s = Server(cls.cloud, extra_env={"DW_LIVE_IDLE": str(cls.IDLE)})
        login(cls.s, "a@example.com")
        daemon_up(cls.s, cls.s.base)

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def setUp(self):
        # An earlier test may have left an upstream in its idle period.
        self.assertTrue(wait_for(lambda: self.events().count("connect") == self.events().count("disconnect"), 10))
        self.start = len(self.s.daemon(self.s.base, "/live-log"))

    def events(self, serial=None):
        log = self.s.daemon(self.s.base, "/live-log")[getattr(self, "start", 0):]
        return [e for e, who in log if serial in (None, who)]

    def open(self, serial, path="live"):
        c = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=30)
        c.request("GET", "/api/camera/%s/%s" % (serial, path))
        return c, c.getresponse()

    def grab(self, serial, seconds):
        """Read the live route for some seconds, then hang up."""
        c, r = self.open(serial)
        data, end = b"", time.time() + seconds
        while time.time() < end:
            data += r.read1(65536)
        r.close()  # the response keeps the socket open until it is closed too
        c.close()
        return r, data

    def test_h264_comes_out_as_mpegts(self):
        r, data = self.grab("SERIAL001", 2.5)
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "video/mp2t"))
        self.assertEqual(data[0], 0x47)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "json", "-"],
                               input=data, capture_output=True, check=True)
        self.assertEqual([(x["codec_name"], x["width"], x["height"]) for x in json.loads(probe.stdout)["streams"]],
                         [("h264", 320, 240)])

    def test_two_clients_share_one_upstream(self):
        (c1, r1), (c2, r2) = self.open("SERIAL001"), self.open("SERIAL001")
        for r in (r1, r2):
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read1(65536)[0], 0x47)
        self.assertEqual(self.events("SERIAL001"), ["connect"])
        for r, c in ((r1, c1), (r2, c2)):
            r.close()
            c.close()

    def test_upstream_closes_after_idle_delay_not_before(self):
        self.grab("SERIAL001", 0.5)
        left = time.time()
        time.sleep(self.IDLE - 0.8)
        self.assertEqual(self.events(), ["connect"])  # still open, ready for a reopen
        self.assertTrue(wait_for(lambda: "disconnect" in self.events(), 5))
        self.assertGreaterEqual(time.time() - left, self.IDLE - 0.1)
        # A reopen inside the delay reuses the upstream and cancels the close.
        self.grab("SERIAL001", 0.3)
        self.grab("SERIAL001", 0.3)
        self.assertEqual(self.events().count("connect"), 2)  # one before, one for the two reopens together

    def test_jpeg_passes_multipart_through(self):
        c, r = self.open("SERIAL002")
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "multipart/x-mixed-replace; boundary=frame"))
        data = b""
        while data.count(b"--frame") < 3:
            data += r.read1(65536)
        r.close()
        c.close()
        self.assertTrue(data.startswith(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "))
        self.assertIn(b"\xff\xd8\xff", data)

    def test_snapshot_during_live_opens_no_second_upstream(self):
        for serial in ("SERIAL001", "SERIAL002"):
            c, r = self.open(serial)
            r.read1(65536)
            time.sleep(1)  # the mock answers 409 and logs a "snapshot" event if one reaches it
            st, body = self.s.req("GET", "/api/camera/%s/snapshot" % serial)
            self.assertEqual(st, 200, body[:200])
            self.assertEqual(body[:3], b"\xff\xd8\xff")
            self.assertEqual(self.events(serial), ["connect"])  # no second upstream, no snapshot at the bridge
            r.close()
            c.close()
            time.sleep(0.5)  # no viewer left, upstream in its idle period: still no bridge snapshot
            self.assertEqual(self.s.req("GET", "/api/camera/%s/snapshot" % serial)[0], 200)
            self.assertEqual(self.events(serial), ["connect"])
            self.setUp()  # wait for the idle close before the next serial


def count(path):
    try:
        return read(path).count("start")
    except OSError:
        return 0


class CameraRestartTest(unittest.TestCase):
    """A camera URL timeout restarts the daemon only when the printer does not answer a pushall probe (mock controls: see mock_daemon.py).
    The mock status is idle: it changes only after a pushall, unless MOCK_IGNORE_PUSHALL exists."""
    GRACE = "0"

    def setUp(self):
        self.cloud = mock_cloud.start()
        self.dir = tempfile.TemporaryDirectory()
        self.starts, self.ignore, self.snapshot = (os.path.join(self.dir.name, n) for n in ("starts", "ignore", "snapshot"))
        env = {"MOCK_STARTS": self.starts, "MOCK_IGNORE_PUSHALL": self.ignore, "MOCK_SNAPSHOT": self.snapshot, "MOCK_SLOW": "4",
               "DW_STATUS_SAMPLE": "0.3", "DW_PROBE_WAIT": "2", "DW_RESTART_BACKOFF": "30", "DW_START_GRACE": self.GRACE}
        self.s = Server(self.cloud, extra_env=env)
        login(self.s, "a@example.com")
        daemon_up(self.s, self.s.base)
        self.assertEqual(count(self.starts), 1)

    def tearDown(self):
        self.s.close()
        self.dir.cleanup()

    def pushalls(self):
        return [c for c in self.s.daemon(self.s.base, "/commands") if c.get("pushing", {}).get("command") == "pushall"]

    def snap(self, path="/api/camera/SERIAL001/snapshot"):
        c = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=30)
        c.request("GET", path)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data, r.getheader("X-Snapshot-Age")

    def test_a_idle_printer_that_answers_the_probe_is_not_restarted(self):
        for _ in range(2):
            self.assertEqual(self.snap()[0], 502)
        self.assertTrue(wait_for(lambda: self.pushalls(), 5))  # the status did not change by itself: the bridge probed
        time.sleep(3)
        self.assertEqual(count(self.starts), 1)
        self.assertNotIn("restarting daemon", self.s.logtext())
        self.assertRegex(self.s.logtext(), r"camera timeout \d+s after daemon start: will probe")
        self.assertRegex(self.s.logtext(), r"camera timeout probe: status answered in \d+\.\ds, no restart")

    def test_a2_no_probe_without_a_camera_timeout(self):
        time.sleep(2)
        self.assertEqual(self.pushalls(), [])

    def test_b_silent_printer_restarts_once_then_waits_for_the_backoff(self):
        open(self.ignore, "w").close()
        self.assertEqual(self.snap()[0], 502)
        self.assertTrue(wait_for(lambda: count(self.starts) == 2, 10))
        daemon_up(self.s, self.s.base)
        self.assertEqual(self.snap()[0], 502)
        time.sleep(4)  # a probe would run and fail, but the backoff is 30 s
        self.assertEqual(count(self.starts), 2)
        self.assertEqual(self.s.logtext().count("restarting daemon"), 1)
        self.assertIn("camera timeout probe: no status in 2s, restarting", self.s.logtext())

    def test_c_restart_waits_while_a_print_upload_is_in_flight(self):
        open(self.ignore, "w").close()
        result = []
        body = {"serial": "SERIAL001", "filename": "slow.gcode.3mf", "file_b64": base64.b64encode(b"x").decode()}
        t = threading.Thread(target=lambda: result.append(self.s.req("POST", "/api/print", body)[0]))
        t.start()
        time.sleep(0.5)
        self.assertEqual(self.snap()[0], 502)
        time.sleep(3)  # longer than the probe wait; the print runs for 4 s
        self.assertEqual(count(self.starts), 1)
        self.assertEqual(self.pushalls(), [])  # no probe while busy
        t.join()
        self.assertEqual(result, [200])
        self.assertTrue(wait_for(lambda: count(self.starts) == 2, 10))  # now it probes, gets no answer and restarts

    def test_d_second_snapshot_within_30_s_comes_from_the_cache(self):
        open(self.snapshot, "w").close()
        st, first, age = self.snap()
        self.assertEqual((st, first[:3]), (200, b"\xff\xd8\xff"))
        self.assertEqual(float(age), 0)
        time.sleep(0.3)
        st, second, age = self.snap()
        self.assertEqual((st, second), (200, first))
        self.assertTrue(0.2 < float(age) < 5, age)
        results = []
        ts = [threading.Thread(target=lambda: results.append(self.snap()[0])) for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(results, [200] * 3)
        calls = [e for e, who in self.s.daemon(self.s.base, "/live-log") if e == "snapshot"]
        self.assertEqual(len(calls), 1)  # one daemon call for five requests


class CameraGraceTest(CameraRestartTest):
    GRACE = "60"

    def test_f_timeout_soon_after_the_daemon_start_is_ignored(self):
        open(self.ignore, "w").close()
        self.assertEqual(self.snap()[0], 502)
        time.sleep(3)
        self.assertEqual((count(self.starts), self.pushalls()), (1, []))
        self.assertIn("ignored", self.s.logtext())

    test_a_idle_printer_that_answers_the_probe_is_not_restarted = None
    test_b_silent_printer_restarts_once_then_waits_for_the_backoff = None
    test_c_restart_waits_while_a_print_upload_is_in_flight = None
    test_d_second_snapshot_within_30_s_comes_from_the_cache = None


class DaemonOutageTest(unittest.TestCase):
    """A print start waits for a daemon that is restarting."""

    def setUp(self):
        self.cloud = mock_cloud.start()
        self.dir = tempfile.TemporaryDirectory()
        self.gate = os.path.join(self.dir.name, "gate")  # while it exists the daemon does not start
        cmd = "sh -c 'while [ -f %s ]; do sleep 0.1; done; exec %s -I %s \"$1\" \"$2\"' sh" % (
            self.gate, sys.executable, os.path.join(HERE, "mock_daemon.py"))
        self.s = Server(self.cloud, cmd, extra_env={"DW_RETRY_WINDOW": "4"})
        login(self.s, "a@example.com")
        daemon_up(self.s, self.s.base)
        self.body = {"serial": "SERIAL001", "filename": "a.gcode.3mf", "file_b64": base64.b64encode(b"x").decode()}

    def tearDown(self):
        self.s.close()
        self.dir.cleanup()

    def crash(self):
        open(self.gate, "w").close()
        os.kill(self.s.daemon(self.s.base, "/health")["pid"], signal.SIGKILL)
        def down():
            try:
                self.s.daemon(self.s.base, "/health")
            except OSError:
                return True
        self.assertTrue(wait_for(down, 5))

    def test_e_print_waits_for_a_short_outage_and_fails_after_the_window(self):
        self.crash()
        threading.Timer(1.5, os.unlink, [self.gate]).start()
        t0 = time.time()
        st, data = self.s.req("POST", "/api/print", self.body)
        self.assertEqual(st, 200, data)
        self.assertGreaterEqual(time.time() - t0, 1.2)
        daemon_up(self.s, self.s.base)
        self.assertEqual(len(self.s.daemon(self.s.base, "/prints")), 1)
        self.crash()  # the daemon stays down this time
        t0 = time.time()
        st, data = self.s.req("POST", "/api/print", self.body)
        self.assertEqual((st, json.loads(data)), (503, {"error": "daemon not reachable"}))
        self.assertGreaterEqual(time.time() - t0, 3.4)
        os.unlink(self.gate)


class MigrationTest(unittest.TestCase):
    OLD_TOKEN = "TOKEN-OLD-V01-SECRET"

    @classmethod
    def v01(cls, data):
        os.makedirs(data)
        with open(os.path.join(data, "credentials.toml"), "w") as f:
            f.write('[cloud]\ntoken = "%s"\nrefresh_token = "REFRESH-OLD-V01"\nuid = "1"\nemail = "Tilman@Example.com"\n'
                    '\n[printers.h2s]\nserial = "H2S-SERIAL"\n' % cls.OLD_TOKEN)
        with open(os.path.join(data, "printers.json"), "w") as f:
            json.dump([{"dev_id": "H2S-SERIAL", "name": "H2S", "dev_access_code": "ACCESS-OLD"}], f)
        os.chmod(os.path.join(data, "credentials.toml"), 0o644)  # v0.1 files are 600; prove we enforce it anyway

    def test_v01_layout_becomes_one_working_account_without_login(self):
        cloud = mock_cloud.start()
        s = Server(cloud, setup=self.v01)
        try:
            id = "tilman_example_com"
            self.assertFalse(os.path.exists(os.path.join(s.data, "credentials.toml")))
            self.assertFalse(os.path.exists(os.path.join(s.data, "printers.json")))
            self.assertEqual(os.listdir(os.path.join(s.data, "accounts")), [id])
            for name in ("credentials.toml", "printers.json"):
                self.assertEqual(stat.S_IMODE(os.stat(s.acct(id, name)).st_mode), 0o600)
            self.assertTrue(os.path.isdir(s.acct(id, "home")))
            h = daemon_up(s, s.base)
            self.assertEqual(h["credentials"], s.acct(id, "credentials.toml"))
            self.assertEqual(h["home"], s.acct(id, "home"))
            _, p = s.json("GET", "/api/printers")
            self.assertEqual([(x["serial"], x["account"], x["email"]) for x in p],
                             [("H2S-SERIAL", id, "Tilman@Example.com")])
            self.assertEqual(s.json("GET", "/api/status/H2S-SERIAL")[1]["served_by"], s.acct(id, "credentials.toml"))
            self.assertEqual(cloud.calls, [])  # no login happened
            for body in s.bodies:
                self.assertNotIn(self.OLD_TOKEN.encode(), body)
            self.assertNotIn(self.OLD_TOKEN, s.logtext())
            self.assertNotIn(b"ACCESS-OLD", b"".join(s.bodies))
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
