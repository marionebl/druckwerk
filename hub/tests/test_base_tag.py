"""The base image tag (hub/tests/base_tag.sh) must change only with what the base image reads."""
import os
import shutil
import subprocess

HUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FILES = ["tests/base_tag.sh", "base.Dockerfile", "externals.env", "externals.py", "bridge/bridge.patch", "slicer/engine/build.sh"]


def tag(repo):
    return subprocess.run([os.path.join(repo, "hub/tests/base_tag.sh")], capture_output=True, text=True, check=True).stdout.strip()


def make_repo(tmp_path):
    for f in FILES:
        dst = tmp_path / "hub" / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(os.path.join(HUB, f), dst)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return str(tmp_path)


def edit(repo, old, new):
    p = os.path.join(repo, "hub/externals.py")
    text = open(p).read()
    assert old in text
    open(p, "w").write(text.replace(old, new, 1))


def test_tag_ignores_the_plugin_hash(tmp_path):
    repo = make_repo(tmp_path)
    before = tag(repo)
    edit(repo, 'PLUGIN_SHA256 = "2450', 'PLUGIN_SHA256 = "2451')
    assert tag(repo) == before


def test_tag_follows_the_printers(tmp_path):
    repo = make_repo(tmp_path)
    before = tag(repo)
    edit(repo, '"preset": "Bambu Lab H2S 0.4 nozzle"', '"preset": "Bambu Lab H2S 0.4 nozzle x"')
    assert tag(repo) != before
