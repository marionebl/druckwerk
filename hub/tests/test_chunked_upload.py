"""Models and G-code go up in parts of at most 1 MB: a proxy in front of the app can refuse bigger bodies."""
import os

from hubfakes import USER

PART = 512 * 1024


def test_model_in_parts_is_joined_and_the_job_waits_until_complete(hub):
    data = os.urandom(PART * 2 + 1000)
    st, job = hub.req("POST", "/api/jobs?name=big.stl&total=%d" % len(data), raw=data[:PART], user=USER)
    assert st == 200 and job["state"] == "uploading" and job["received"] == PART
    jid = job["id"]
    # A part with the wrong offset is refused and says where to go on.
    st, err = hub.req("POST", "/api/jobs/%s/model?offset=7&total=%d" % (jid, len(data)), raw=data[PART:2 * PART], user=USER)
    assert st == 409 and err["received"] == PART
    st, job = hub.req("POST", "/api/jobs/%s/model?offset=%d&total=%d" % (jid, PART, len(data)), raw=data[PART:2 * PART], user=USER)
    assert st == 200 and job["state"] == "uploading"
    st, job = hub.req("POST", "/api/jobs/%s/model?offset=%d&total=%d" % (jid, 2 * PART, len(data)), raw=data[2 * PART:], user=USER)
    assert st == 200 and job["state"] == "uploaded" and "total" not in job
    assert hub.req("GET", "/api/jobs/%s/model" % jid, user=USER)[1] == data


def test_a_part_over_1_mb_and_a_foreign_job_are_refused(hub):
    st, job = hub.req("POST", "/api/jobs?name=big.stl&total=5000000", raw=b"x" * PART, user=USER)
    jid = job["id"]
    assert hub.req("POST", "/api/jobs/%s/model?offset=%d&total=5000000" % (jid, PART), raw=b"x" * (1024 * 1024 + 1), user=USER)[0] == 413
    other = "u-other"
    assert hub.req("POST", "/api/jobs/%s/model?offset=%d&total=5000000" % (jid, PART), raw=b"x" * PART, user=other)[0] in (401, 403)
    assert hub.req("POST", "/api/jobs/%s/model?offset=%d&total=5000000" % (jid, PART), raw=b"x" * PART)[0] == 401


def test_whole_file_upload_still_works(hub):
    st, job = hub.req("POST", "/api/jobs?name=cube.stl", raw=b"solid x", user=USER)
    assert st == 200 and job["state"] == "uploaded" and "received" not in job
