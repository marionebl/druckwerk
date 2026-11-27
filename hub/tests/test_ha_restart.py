"""A restarting Home Assistant is a 503 with retry, so the UI can say so and try again."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ha import HA, RESTARTING, HAError, HAUnavailable
from hubfakes import ADMIN


def gateway(code, body=b"<html>502: Bad Gateway</html>"):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize("code", [502, 503, 504])
def test_gateway_errors_mean_restarting(code):
    srv = gateway(code)
    try:
        ha = HA("http://127.0.0.1:%d" % srv.server_address[1], "ws://127.0.0.1:1/x", "t")
        with pytest.raises(HAUnavailable, match="restarting"):
            ha._rest("GET", "/api/states")
    finally:
        srv.shutdown()


def test_other_errors_keep_their_text():
    srv = gateway(401, json.dumps({"message": "Unauthorized"}).encode())
    try:
        ha = HA("http://127.0.0.1:%d" % srv.server_address[1], "ws://127.0.0.1:1/x", "t")
        with pytest.raises(HAError, match="answered 401: Unauthorized") as e:
            ha._rest("GET", "/api/states")
        assert not isinstance(e.value, HAUnavailable)
    finally:
        srv.shutdown()


def test_unreachable_home_assistant_means_restarting():
    ha = HA("http://127.0.0.1:1", "ws://127.0.0.1:1/x", "t")
    with pytest.raises(HAUnavailable):
        ha._rest("GET", "/api/states")


def test_hub_answers_503_with_retry_while_home_assistant_restarts(hub):
    hub.setup_printers()

    def down(max_age=3):
        raise HAUnavailable(RESTARTING)
    hub.ha.states = down
    st, body = hub.req("GET", "/api/printers", user=ADMIN)
    assert st == 503 and body == {"error": RESTARTING, "retry": True}


def test_other_ha_errors_stay_a_502_without_retry(hub):
    hub.setup_printers()

    def broken(max_age=3):
        raise HAError("Home Assistant answered 500: boom")
    hub.ha.states = broken
    st, body = hub.req("GET", "/api/printers", user=ADMIN)
    assert st == 502 and "retry" not in body
