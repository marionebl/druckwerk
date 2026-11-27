"""End-to-end test of the slicer app's HTTP API with a fake slicer, the bridge and HA core."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import externals  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config><plate><metadata key="index" value="1"/><metadata key="prediction" value="844"/><metadata key="weight" value="3.72"/>
<object identify_id="1" name="cube20" skipped="false"/>
<filament id="1" type="PETG" color="#ADB1B2" used_m="1.2" used_g="3.72"/></plate></config>"""
GCODE = "; CHANGE_LAYER\n; Z_HEIGHT: 0.2\n; FEATURE: Outer wall\nG1 X10 Y10\nG1 X30 Y10 E1\nG1 X30 Y30 E1\n"


def minimal_slice(path):
    """A .gcode.3mf with only the parts the app reads, like the cube20 slice of the slicer 2.9.2."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", SLICE_INFO)
        z.writestr("Metadata/plate_1.gcode", GCODE)
        z.writestr("Metadata/plate_1.png", b"\x89PNG\r\n\x1a\n")
    return str(path)


PRESETS = {"model": "H2S", "machine": {"name": "Bambu Lab H2S 0.4 nozzle", "printable_area": ["0x0", "350x0", "350x320", "0x320"]},
           "process": {q: {"name": "proc " + q} for q in ("good", "better", "best")},
           "filaments": [{"name": "Generic PETG", "id": "GFG99", "type": "PETG", "n": 9, "config": {"name": "Generic PETG"}},
                         {"name": "Acme PETG HF @BBL H2S", "id": "GFG02", "type": "PETG", "n": 1, "config": {"name": "Acme PETG HF"}},
                         {"name": "Acme PLA Matte @BBL H2S", "id": "GFX01", "type": "PLA", "n": 1, "config": {"name": "Acme PLA Matte"}},
                         {"name": "Generic PLA", "id": "GFL99", "type": "PLA", "n": 9, "config": {"name": "Generic PLA"}}]}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(base, method, path, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture
def app(tmp_path):
    result = os.environ.get("FAKE_RESULT") or minimal_slice(tmp_path / "result.gcode.3mf")
    mport, sport = free_port(), free_port()
    (tmp_path / "engine" / "presets").mkdir(parents=True)
    (tmp_path / "engine" / "presets" / "H2S.json").write_text(json.dumps(PRESETS))
    (tmp_path / "engine" / "version.json").write_text('{"tag": "t"}')
    (tmp_path / "engine" / "t").mkdir()
    (tmp_path / "engine" / "t" / "engine-mt.wasm").write_bytes(b"\0asm")
    mocks = subprocess.Popen([sys.executable, os.path.join(HERE, "mocks.py"), str(mport)])
    env = dict(os.environ, SLICER_JOBS=str(tmp_path / "jobs"), SLICER_SHARE=str(tmp_path / "share"),
               SLICER_SENDER_URL="http://127.0.0.1:%d" % mport, SLICER_CORE_URL="http://127.0.0.1:%d/core" % mport,
               SLICER_SLICE_CMD=os.path.join(HERE, "fake_slice.sh"), SLICER_BS=os.path.join(HERE, "fake_bs.py"),
               SLICER_PORT=str(sport), SLICER_ENGINE=str(tmp_path / "engine"), SUPERVISOR_TOKEN="tok", FAKE_RESULT=result)
    server = subprocess.Popen([sys.executable, os.path.join(APP, "server.py")], env=env)
    base = "http://127.0.0.1:%d" % sport
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/api/printers", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    yield base, "http://127.0.0.1:%d" % mport, tmp_path, result
    server.kill()
    mocks.kill()


def wait_done(base, jid):
    for _ in range(100):
        job = json.loads(call(base, "GET", "/api/jobs/" + jid)[1])
        if job["state"] in ("done", "failed"):
            return job
        time.sleep(0.1)
    raise AssertionError("slice did not finish")


def test_slicing_job_shows_the_step_and_percent_from_the_slicer_log(app):
    base = app[0]
    jid = json.loads(call(base, "POST", "/api/jobs?name=steps.stl", raw=b"solid x\nendsolid x\n")[1])["id"]
    assert call(base, "POST", "/api/jobs/%s/slice" % jid, {"serial": "H2S0001", "quality": "best", "tray": "0:0"})[0] == 202
    for _ in range(40):
        job = json.loads(call(base, "GET", "/api/jobs/" + jid)[1])
        if job.get("progress"):
            break
        time.sleep(0.1)
    assert job["state"] == "slicing" and job["progress"] == {"percent": 35, "message": "Generating infill"}, job
    done = wait_done(base, jid)
    assert done["state"] == "done" and done["progress"]["percent"] == 100


def test_slice_progress_reads_the_last_step_line_and_ignores_config_lines():
    sys.path.insert(0, APP)
    import server
    log = "[info] sparse_infill_density = 15%\n  5Processing triangulated mesh\n 20% Generating perimeters\n[info] x 50 Z\n"
    assert server.slice_progress(log) == {"percent": 20, "message": "Generating perimeters"}
    assert server.slice_progress("[info] sparse_infill_density = 15%\n") is None
    assert server.slice_progress("150Nonsense\n") is None


def test_printers_lists_supported_models_with_loaded_trays(app):
    base = app[0]
    status, raw = call(base, "GET", "/api/printers")
    data = json.loads(raw)
    assert status == 200
    assert [p["model"] for p in data["printers"]] == ["H2S"]  # X1C has no profile table
    assert [t["name"] for t in data["printers"][0]["trays"]] == ["PETG HF", "PLA Matte"]  # empty slot 3 left out
    assert data["printers"][0]["trays"][0]["colour"] == "#ADB1B2"


def test_upload_slice_preview_print(app):
    base, mock, tmp, result = app
    status, raw = call(base, "POST", "/api/jobs?name=Fan%20test.stl", raw=b"solid x\nendsolid x\n")
    assert status == 200, raw
    jid = json.loads(raw)["id"]
    assert call(base, "GET", "/api/jobs/%s/model" % jid) == (200, b"solid x\nendsolid x\n")  # Reslice downloads it again
    assert call(base, "GET", "/api/jobs/000000000000/model")[0] == 404
    status, raw = call(base, "POST", "/api/jobs/%s/slice" % jid, {"serial": "H2S0001", "quality": "best", "tray": "0:0"})
    assert status == 202, raw
    job = wait_done(base, jid)
    assert job["state"] == "done", job
    assert job["info"]["seconds"] > 0 and job["info"]["grams"] > 0
    got = open(tmp / "jobs" / jid / "slice-env.txt").read().split("\n")
    assert got[:5] == ["PRINTER=" + externals.PRINTERS["H2S"]["preset"], "PROCESS=" + externals.PRINTERS["H2S"]["quality"]["best"],
                       "BED=High Temp Plate", "COLOURS=#ADB1B2", "FILAMENT=Acme PETG HF @BBL H2S"]
    status, gcode = call(base, "GET", "/api/jobs/%s/plate.gcode" % jid)
    assert status == 200 and b"G1 " in gcode
    # No print without the confirm flag.
    status, raw = call(base, "POST", "/api/jobs/%s/print" % jid, {})
    assert status == 400
    assert json.loads(call(mock, "GET", "/calls")[1]) == []
    status, raw = call(base, "POST", "/api/jobs/%s/print" % jid, {"confirm": True})
    assert status == 200, raw
    calls = json.loads(call(mock, "GET", "/calls")[1])
    assert len(calls) == 1
    assert calls[0]["path"] == "/core/services/druckwerk/print_file?return_response"
    assert calls[0]["auth"] == "Bearer tok"
    sent = calls[0]["body"]
    assert sent["serial"] == "H2S0001" and sent["force"] is False and sent["use_ams"] is True
    assert sent["file"] == str(tmp / "share" / "Fan test_best_h2s.gcode.3mf")
    assert os.path.getsize(sent["file"]) == os.path.getsize(result)


def test_slice_rejects_unknown_tray_and_quality(app):
    base = app[0]
    jid = json.loads(call(base, "POST", "/api/jobs?name=steps.stl", raw=b"x")[1])["id"]
    assert call(base, "POST", "/api/jobs/%s/slice" % jid, {"serial": "H2S0001", "quality": "better", "tray": "0:2"})[0] == 400
    assert call(base, "POST", "/api/jobs/%s/slice" % jid, {"serial": "H2S0001", "quality": "ultra", "tray": "0:0"})[0] == 400
    assert call(base, "POST", "/api/jobs?name=a.exe", raw=b"x")[0] == 400


def gcode_query(**kw):
    q = {"serial": "H2S0001", "quality": "better", "tray": "0:0", "bed": "High Temp Plate", "filament": "Acme PETG HF @BBL H2S",
         "seconds": "1234", "grams": "5.5", "metres": "1.8", "engine": "mt", "slice_s": "12.3"}
    q.update(kw)
    return "&".join("%s=%s" % (k, v.replace(" ", "%20").replace("@", "%40")) for k, v in q.items())


def test_browser_gcode_becomes_a_normal_slice(app):
    base, mock, tmp, _ = app
    gcode = b"; printer_model = Bambu Lab H2S\n; CHANGE_LAYER\n; Z_HEIGHT: 0.2\n; FEATURE: Outer wall\nG1 X10 Y10\nG1 X30 Y10 E1\n"
    jid = json.loads(call(base, "POST", "/api/jobs?name=Fan.stl", raw=b"solid x")[1])["id"]
    status, raw = call(base, "POST", "/api/jobs/%s/gcode?%s" % (jid, gcode_query()), raw=gcode)
    assert status == 200, raw
    job = json.loads(raw)
    assert job["state"] == "done" and job["info"]["seconds"] == 1234 and job["info"]["grams"] == 5.5
    assert job["info"]["has_png"] is False and job["settings"]["filament"] == "Acme PETG HF @BBL H2S"
    assert call(base, "GET", "/api/jobs/%s/plate.gcode" % jid)[1] == gcode
    with zipfile.ZipFile(job_result(tmp, jid)) as z:
        settings = json.loads(z.read("Metadata/project_settings.config"))
        assert z.read("Metadata/plate_1.gcode") == gcode and 'value="O1S"' in z.read("Metadata/slice_info.config").decode()
    assert settings["curr_bed_type"] == "High Temp Plate"  # Orca refuses PETG without a plate
    assert call(base, "POST", "/api/jobs/%s/print" % jid, {"confirm": True})[0] == 200  # the stored file prints like any slice
    assert json.loads(call(mock, "GET", "/calls")[1])[0]["body"]["file"].endswith("Fan_better_h2s.gcode.3mf")


def job_result(tmp, jid):
    return next((tmp / "jobs" / jid / "out").glob("*.gcode.3mf"))


def test_browser_gcode_rejects_bad_requests(app):
    base = app[0]
    jid = json.loads(call(base, "POST", "/api/jobs?name=steps.stl", raw=b"x")[1])["id"]
    assert call(base, "POST", "/api/jobs/%s/gcode?%s" % (jid, gcode_query(tray="0:2")), raw=b"G1")[0] == 400
    assert call(base, "POST", "/api/jobs/%s/gcode?%s" % (jid, gcode_query(quality="ultra")), raw=b"G1")[0] == 400
    assert call(base, "POST", "/api/jobs/%s/gcode?%s" % (jid, gcode_query(filament="Nope")), raw=b"G1")[0] == 400
    assert call(base, "POST", "/api/jobs/%s/gcode?%s" % (jid, gcode_query(seconds="x")), raw=b"G1")[0] == 400
    assert call(base, "POST", "/api/jobs/%s/gcode?%s" % (jid, gcode_query()), raw=b"")[0] == 413
    assert call(base, "POST", "/api/jobs/aaaaaaaaaaaa/gcode?%s" % gcode_query(), raw=b"G1")[0] == 404


def test_filament_follows_the_trays_profile_id_else_generic(app):
    base = app[0]
    assert json.loads(call(base, "GET", "/api/engine/filament?serial=H2S0001&tray=0:0")[1]) == {"filament": "Acme PETG HF @BBL H2S"}
    assert json.loads(call(base, "GET", "/api/engine/filament?serial=H2S0001&tray=0:1")[1]) == {"filament": "Generic PLA"}  # idx unknown
    assert call(base, "GET", "/api/engine/filament?serial=H2S0001&tray=0:2")[0] == 400


def test_library_spool_slices_with_a_preset_named_like_it(app):
    base, _, tmp, _ = app
    spool = "type=PLA&brand=Acme&name=PLA%20Matte&colour=112233&spool=777"
    assert json.loads(call(base, "GET", "/api/engine/filament?serial=H2S0001&" + spool)[1]) == {"filament": "Acme PLA Matte @BBL H2S"}
    other = "type=PLA&brand=Nobody&name=Silk&colour=112233"
    assert json.loads(call(base, "GET", "/api/engine/filament?serial=H2S0001&" + other)[1]) == {"filament": "Generic PLA"}
    assert call(base, "GET", "/api/engine/filament?serial=H2S0001")[0] == 400  # neither tray nor spool
    jid = json.loads(call(base, "POST", "/api/jobs?name=Fan.stl", raw=b"solid x")[1])["id"]
    q = gcode_query(filament="Acme PLA Matte @BBL H2S").replace("tray=0:0", spool)
    status, raw = call(base, "POST", "/api/jobs/%s/gcode?%s" % (jid, q), raw=b"G1 X1\n")
    assert status == 200, raw
    s = json.loads(raw)["settings"]
    assert s["tray"]["ams"] is None and s["tray"]["colour"] == "#112233" and s["tray"]["spool_id"] == "777"
    assert s["filament"] == "Acme PLA Matte @BBL H2S"


def test_engine_files_and_isolation_headers(app):
    base = app[0]
    import urllib.request
    with urllib.request.urlopen(base + "/engine/t/engine-mt.wasm") as r:
        assert r.headers["Content-Type"] == "application/wasm" and "immutable" in r.headers["Cache-Control"]
        assert r.headers["Cross-Origin-Opener-Policy"] == "same-origin"
        assert r.headers["Cross-Origin-Embedder-Policy"] == "require-corp"
        assert r.headers["Document-Isolation-Policy"] == "isolate-and-require-corp"
        assert r.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    with urllib.request.urlopen(base + "/engine/version.json") as r:
        assert r.headers["Cache-Control"] == "no-cache"
    assert call(base, "GET", "/engine/../../etc/passwd")[0] == 404
    assert call(base, "GET", "/engine/nothing")[0] == 404
    with urllib.request.urlopen(base + "/api/printers") as r:
        assert r.headers["Cross-Origin-Embedder-Policy"] == "require-corp"
