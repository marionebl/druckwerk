"""The web UI files are served at / without identity; nothing else from hub/ui is."""
from hubfakes import ADMIN


def test_index_and_assets_load_without_identity(hub):
    st, body = hub.req("GET", "/")
    assert st == 200 and b"<title>Druckwerk</title>" in body and b'src="app.js"' in body
    for path in ("/index.html", "/style.css", "/app.js", "/api.js", "/util.js", "/views/queue.js", "/views/setup.js", "/feedback.js", "/views/preview.js", "/views/gcodeparse.js", "/views/gcodeworker.js", "/vendor/three.module.min.js", "/vendor/mpegts.js", "/vendor/snapdom.mjs"):
        assert hub.req("GET", path)[0] == 200, path


def test_index_loads_the_feedback_button(hub):
    assert b'src="feedback.js"' in hub.req("GET", "/")[1]  # relative, so it works behind the ingress prefix
    st, body = hub.req("GET", "/feedback.js")
    assert st == 200 and b"api/feedback" in body


def test_only_the_ui_files_are_served(hub):
    for path in ("/check_ui.py", "/mock_server.py", "/views/../app.py", "/views/x.py", "/vendor/x.js", "/vendor/LICENSE-three.txt", "/vendor/LICENSE-mpegts.txt", "/vendor/LICENSE-snapdom.txt", "/vendor/../app.js.bak", "/..%2fapp.py"):
        assert hub.req("GET", path)[0] in (401, 404), path
    assert hub.req("GET", "/api/me")[0] == 401  # the API behind the page still needs identity
    assert hub.req("GET", "/api/me", user=ADMIN)[0] == 200


def test_the_page_scripts_load_no_code_from_a_cdn():
    """feedback.js once imported snapdom from jsdelivr into the Home Assistant origin, with no integrity check."""
    import glob
    import os
    ui = os.path.join(os.path.dirname(__file__), "..", "ui")
    for path in glob.glob(os.path.join(ui, "*.js")) + glob.glob(os.path.join(ui, "views", "*.js")):
        assert "https://" not in open(path).read(), path
    assert os.path.isfile(os.path.join(ui, "vendor", "snapdom.mjs"))
