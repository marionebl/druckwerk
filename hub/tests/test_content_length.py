"""A negative or non-numeric Content-Length is a 400 on every route that reads a body, not a read until the client closes."""
import socket

import pytest

from hubfakes import ADMIN

ROUTES = [  # (method, path, extra headers): the JSON helper, the bridge proxy and the upload
    ("POST", "/api/setup/login/start", {"X-Remote-User-Id": ADMIN, "X-Remote-User-Name": ADMIN}),
    ("POST", "/api/bridge/api/pause/X", {"X-Remote-User-Id": ADMIN, "X-Remote-User-Name": ADMIN}),
    ("PUT", "/api/upload/a.gcode.3mf", {}),  # the app secret is added below
]


def raw_request(port, method, path, length, headers):
    lines = ["%s %s HTTP/1.1" % (method, path), "Host: x", "Content-Length: " + length] + ["%s: %s" % kv for kv in headers.items()]
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + b"{}")
    sock.shutdown(socket.SHUT_WR)  # without the fix the server would wait for more body bytes
    answer = b""
    while chunk := sock.recv(65536):
        answer += chunk
    sock.close()
    return int(answer.split(b" ", 2)[1]), answer


@pytest.mark.parametrize("length", ["-1", "abc", "1e3", " -5"])
@pytest.mark.parametrize("method,path,headers", ROUTES)
def test_bad_content_length_is_400(hub, method, path, headers, length):
    headers = dict(headers, **({"X-Hub-Secret": hub.app.secret} if method == "PUT" else {}))
    status, answer = raw_request(hub.port, method, path, length, headers)
    assert status == 400 and b"non-negative" in answer, answer[:300]
