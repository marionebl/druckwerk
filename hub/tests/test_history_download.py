"""History detail: download the source model (else the sliced 3MF), for people who may see the print."""
import zipfile
import io

from hubfakes import ADMIN, OTHER, USER
from test_history_settings import scene

STL = b"solid cube\nendsolid cube\n"


def with_model(hub, jid):
    with open(hub.s.jobs[jid]["model"], "wb") as f:
        f.write(STL)


def test_model_download_for_an_allowed_user(hub):
    with_model(hub, scene(hub))
    st, body = hub.req("GET", "/api/history/7/file", user=USER)
    assert (st, body) == (200, STL)
    assert hub.req("GET", "/api/history/7/file", user=ADMIN)[0] == 200


def test_download_is_refused_to_others(hub):
    with_model(hub, scene(hub))
    assert hub.req("GET", "/api/history/7/file", user=OTHER)[0] in (403, 404)
    assert hub.req("GET", "/api/history/7/file")[0] in (401, 403)


def test_sliced_3mf_when_the_model_is_gone(hub):
    scene(hub)  # no model file on disk
    st, d = hub.req("GET", "/api/history/7/settings", user=USER)
    assert d["download"]["kind"] == "sliced" and d["download"]["name"].endswith(".gcode.3mf"), d
    st, body = hub.req("GET", "/api/history/7/file", user=USER)
    assert st == 200 and "Metadata/plate_1.gcode" in zipfile.ZipFile(io.BytesIO(body)).namelist()


def test_no_file_no_download(hub):
    scene(hub, slice_alive=False)
    st, d = hub.req("GET", "/api/history/7/settings", user=USER)
    assert d["download"] is None, d
    assert hub.req("GET", "/api/history/7/file", user=USER)[0] == 404
