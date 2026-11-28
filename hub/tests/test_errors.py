"""Failures are reported: an unexpected exception is a 500, a failed step leaves no half-made job, bad input is a 400."""
import os

import pytest

from hubfakes import ADMIN, SERIAL, USER, make_3mf


def broken_share(hub):
    """A share that cannot be created, so the copy after the job was added fails."""
    blocker = hub.tmp / "not_a_dir"
    blocker.write_text("x")
    hub.app.share = str(blocker / "share")


def test_an_unexpected_exception_is_a_500_not_a_dropped_connection(hub):
    assert hub.req("POST", "/api/grants", {}, user=ADMIN)[0] == 500  # a missing field raised TypeError


def test_a_failed_copy_leaves_no_job_when_a_file_is_queued_by_path(hub):
    hub.setup_printers()
    path = hub.media / "x.gcode.3mf"
    make_3mf(path)
    broken_share(hub)
    st, body = hub.req("POST", "/api/queue", {"file": str(path), "target": SERIAL}, user=ADMIN)
    assert st == 500 and "error" in body
    assert hub.req("GET", "/api/queue", user=ADMIN)[1]["jobs"] == []


def test_a_failed_copy_leaves_no_job_when_a_slice_is_queued(hub):
    hub.setup_printers()
    hub.grant(USER, SERIAL)
    jid = hub.sliced(owner=USER)
    broken_share(hub)
    assert hub.req("POST", "/api/jobs/%s/queue" % jid, {"target": SERIAL}, user=USER)[0] == 500
    assert hub.req("GET", "/api/queue", user=ADMIN)[1]["jobs"] == []


def test_a_failure_after_the_copy_removes_the_copied_file_too(hub, monkeypatch):
    hub.setup_printers()
    path = hub.media / "x.gcode.3mf"
    make_3mf(path)

    def boom(*a):
        raise RuntimeError("settings")
    monkeypatch.setattr(hub.app, "save_settings", boom)
    assert hub.req("POST", "/api/queue", {"file": str(path), "target": SERIAL}, user=ADMIN)[0] == 500
    assert hub.req("GET", "/api/queue", user=ADMIN)[1]["jobs"] == []
    assert not os.path.isdir(hub.app.share) or os.listdir(hub.app.share) == []


@pytest.mark.parametrize("bad", [
    {"filaments": "PLA"}, {"filaments": ["PLA"]}, {"filaments": {"type": "PLA"}}, {"filaments": [{"colour": "x"}]},
    {"options": []}, {"options": "fast"}, {"options": {"timelapse": [1]}},
])
def test_wrong_shape_of_filaments_or_options_is_a_400(hub, bad):
    hub.setup_printers()
    hub.seed_share()
    st, body = hub.req("POST", "/api/queue", dict({"file": "a.gcode.3mf", "target": SERIAL}, **bad), user=ADMIN)
    assert st == 400, body
    path = hub.media / "y.gcode.3mf"
    make_3mf(path)
    assert hub.req("POST", "/api/queue", dict({"file": str(path), "target": SERIAL}, **bad), user=ADMIN)[0] == 400
    assert hub.req("GET", "/api/queue", user=ADMIN)[1]["jobs"] == []


def test_a_client_that_went_away_is_one_log_line(hub, capsys):
    class Gone:
        def write(self, data):
            raise BrokenPipeError()

    h = hub.s.Handler.__new__(hub.s.Handler)
    h.wfile, h.command, h.path = Gone(), "GET", "/api/queue?x=1"
    h.request_version, h.requestline, h.close_connection = "HTTP/1.1", "GET /", False
    h._headers_buffer = []
    h.send(200, {"a": 1})  # raised BrokenPipeError out of the handler
    err = capsys.readouterr().err
    assert err.count("client went away") == 1 and "Traceback" not in err and "x=1" not in err
