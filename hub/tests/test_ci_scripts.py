"""The repository's CI check scripts (tools/), run against small throwaway git repositories."""
import json
import os
import shutil
import subprocess

TOOLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools")
MANIFEST = "custom_components/druckwerk/manifest.json"


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True)


def commit(repo, files):
    for path, text in files.items():
        p = os.path.join(repo, path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "x")


def make_repo(tmp_path):
    repo = str(tmp_path)
    git(repo, "init", "-q", "-b", "main")
    commit(repo, {MANIFEST: json.dumps({"version": "1.0.0"}), "custom_components/druckwerk/button.py": "a = 1\n"})
    git(repo, "checkout", "-q", "-b", "pr")
    return repo


def run(repo):
    return subprocess.run(["bash", os.path.join(TOOLS, "check-integration-version.sh"), "main"], cwd=repo, capture_output=True, text=True)


def test_integration_change_without_a_new_version_fails(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, {"custom_components/druckwerk/button.py": "a = 2\n"})
    r = run(repo)
    assert r.returncode == 1 and "still 1.0.0" in r.stdout, r.stdout + r.stderr


def test_integration_change_with_a_new_version_passes(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, {"custom_components/druckwerk/button.py": "a = 2\n", MANIFEST: json.dumps({"version": "1.0.1"})})
    assert run(repo).returncode == 0


def test_a_change_elsewhere_passes(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, {"README.md": "x\n"})
    assert run(repo).returncode == 0


def test_a_version_raised_in_an_earlier_commit_of_the_pull_request_counts(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, {MANIFEST: json.dumps({"version": "1.0.1"})})
    commit(repo, {"custom_components/druckwerk/button.py": "a = 3\n"})
    assert run(repo).returncode == 0


def test_check_externals_fails_on_a_wrong_hash(tmp_path):
    """tools/check-externals.sh against a local server: a changed byte in the pinned hash must fail."""
    import functools
    import http.server
    import threading
    srv = tmp_path / "srv"
    srv.mkdir()
    (srv / "plugin.zip").write_bytes(b"plugin")
    (srv / "cert.cer").write_bytes(b"cert")
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(srv)))
    httpd.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    import hashlib
    good = hashlib.sha256(b"plugin").hexdigest()
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "hub").mkdir()
    shutil.copy(os.path.join(TOOLS, "check-externals.sh"), repo / "tools")

    def run_with(sha):
        (repo / "hub" / "externals.py").write_text('PLUGIN_URL = "%s/plugin.zip"\nPLUGIN_SHA256 = "%s"\nCERT_URL = "%s/cert.cer"\n' % (base, sha, base))
        return subprocess.run(["bash", str(repo / "tools" / "check-externals.sh")], capture_output=True, text=True)
    try:
        assert run_with(good).returncode == 0
        bad = run_with(("0" if good[0] != "0" else "1") + good[1:])
        assert bad.returncode == 1 and "plugin SHA-256" in bad.stdout, bad.stdout + bad.stderr
    finally:
        httpd.shutdown()
