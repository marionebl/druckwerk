"""Plates inbox: a private GitHub repo that cloud agents push sliced plates to, polled into the print queue.

Layout: plates/<id>/<name>.gcode.3mf, optionally plates/<id>/job.json. A plate that is added to the queue is removed from
the repo. Nothing here starts a print.
"""
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from app import log
from gcode_guard import check_gcode_3mf, file_model, same_model
from queue_access import Caller, Forbidden
from queue_engine import QueueError
from queue_registry import write_json

API = "https://api.github.com"
DEFAULT_REPO = "marionebl/druckwerk-plates"
EVERY = 60  # seconds between polls
MAX_BYTES = 100 * 1024 * 1024
MAX_META = 64 * 1024
TAKEN_KEEP = 500
SAFE = re.compile(r"[A-Za-z0-9._-]{1,100}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class Rejected(Exception):
    """This file will never be accepted as it is; it stays in the repo and is not fetched again."""


class GitHubError(Exception):
    pass


def load_options(data_dir):
    """The app's options (Supervisor writes them to <data>/options.json); a missing file means the defaults."""
    try:
        with open(os.path.join(data_dir, "options.json")) as f:
            o = json.load(f)
    except (OSError, ValueError):
        o = {}
    return {"repo": str(o.get("plates_repo") or DEFAULT_REPO).strip(), "token": str(o.get("plates_token") or "").strip()}


class PlatesInbox:
    def __init__(self, app, repo, token, data_dir, api=API, clock=time.time):
        self.app, self.repo, self.token, self.api, self.clock = app, repo, token, api.rstrip("/"), clock
        self.dir = os.path.join(data_dir, "plates-inbox")
        self.taken_path = os.path.join(data_dir, "plates_taken.json")
        self.taken = []  # "<id>/<file>" of plates that became a job, so a failed delete cannot make a second one
        try:
            with open(self.taken_path) as f:
                self.taken = list(json.load(f))
        except (OSError, ValueError):
            pass
        self.rejected = {}  # (key, blob sha) -> reason, in memory: a changed file or a restart looks again
        self.rejected_dirs = {}  # folder -> its id (GitHub's tree sha) when every file in it was rejected: skipped until the id changes
        self.last_poll, self.last_error = None, None
        self.enabled = bool(token) and bool(REPO.fullmatch(repo or ""))

    @classmethod
    def from_options(cls, app, data_dir):
        o = load_options(data_dir)
        return cls(app, o["repo"], o["token"], data_dir)

    def status(self):
        """For the Setup view; never the token."""
        return {"enabled": self.enabled, "repo": self.repo if self.enabled else "", "last_poll": self.last_poll,
                "last_error": self.last_error, "taken": len(self.taken)}

    def run(self, stop):
        if not self.enabled:
            return log("plates inbox is off (no plates_token)")
        log("plates inbox polls %s every %d s" % (self.repo, EVERY))
        while True:
            self.poll()
            if stop.wait(EVERY):
                return

    # -- github --
    def _open(self, method, path, accept, body=None, timeout=30):
        req = urllib.request.Request(self.api + "/repos/%s/%s" % (self.repo, quote(path, safe="/?=&")), method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + self.token, "Accept": accept,
                                              "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "druckwerk"})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            e.close()
            raise GitHubError("GitHub answered %d for %s %s" % (e.code, method, path.split("?")[0]))
        except (urllib.error.URLError, OSError) as e:
            raise GitHubError("GitHub not reachable: %s" % getattr(e, "reason", e))  # the message never holds the token

    def _list(self, path):
        try:
            with self._open("GET", "contents/" + path, "application/vnd.github+json") as r:
                data = json.loads(r.read())
        except GitHubError as e:
            if " 404 " in str(e):
                return []  # no plates/ yet, or the folder was just deleted
            raise
        return data if isinstance(data, list) else []

    def _blob(self, entry, dest):
        """Write the file's content to dest; the blobs API serves up to 100 MB, which the contents API does not."""
        with self._open("GET", "git/blobs/" + entry["sha"], "application/vnd.github.raw+json", timeout=120) as r, \
                open(dest, "wb") as out:
            n = 0
            while chunk := r.read(1 << 20):
                n += len(chunk)
                if n > MAX_BYTES:
                    raise Rejected("too large: over %d MB" % (MAX_BYTES >> 20))
                out.write(chunk)

    def _delete(self, entry):
        self._open("DELETE", "contents/" + entry["path"], "application/vnd.github+json",
                   {"message": "Druckwerk took %s" % entry["path"], "sha": entry["sha"]}).close()

    # -- one poll --
    def poll(self):
        """One pass over plates/. Returns the number of jobs added. Never raises."""
        if not self.enabled:
            return 0
        added, errors = 0, []
        self.last_poll = self.clock()
        try:
            for d in self._list("plates"):
                if d.get("type") != "dir" or not SAFE.fullmatch(d.get("name", "")):
                    continue
                if d.get("sha") and self.rejected_dirs.get(d["name"]) == d["sha"]:
                    continue
                try:
                    added += self._folder(d["name"], errors, d.get("sha"))
                except Exception as e:  # noqa: BLE001 - one bad folder must not stop the others
                    errors.append("plates/%s: %s" % (d["name"], e))
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
        self.last_error = errors[0] if errors else None
        for e in errors:
            log("plates inbox: " + e)
        return added

    def _folder(self, id, errors, tree_id):
        files = [f for f in self._list("plates/" + id) if f.get("type") == "file"]
        plates = [f for f in files if f["name"].endswith(".gcode.3mf") and SAFE.fullmatch(f["name"])]
        if not plates:
            return 0  # an upload still in progress, or only job.json
        meta = self._meta(next((f for f in files if f["name"] == "job.json"), None))
        added, keep = 0, False
        for f in plates:
            key = "%s/%s" % (id, f["name"])
            memo = (key, f["sha"])
            if key in self.taken:
                continue
            if memo in self.rejected:
                keep = True
                continue
            try:
                self._add(f, meta)
            except Rejected as e:
                self.rejected[memo] = str(e)
                errors.append("%s: %s" % (key, e))
                keep = True
                continue
            self.taken = (self.taken + [key])[-TAKEN_KEEP:]
            write_json(self.taken_path, self.taken)
            added += 1
            log("plates inbox: queued %s" % key)
        if keep and tree_id:
            self.rejected_dirs[id] = tree_id
        if not keep:
            for f in files:  # the folder disappears with its last file
                try:
                    self._delete(f)
                except GitHubError as e:
                    errors.append("%s: %s (taken, deleted at the next poll)" % (id, e))
                    break
        return added

    def _meta(self, entry):
        if not entry or entry.get("size", 0) > MAX_META:
            return {}
        os.makedirs(self.dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir)
        os.close(fd)
        try:
            self._blob(entry, tmp)
            with open(tmp) as f:
                m = json.load(f)
            return m if isinstance(m, dict) else {}
        except (Rejected, ValueError, OSError):
            return {}  # a broken job.json does not block the plate; defaults apply
        finally:
            os.remove(tmp)

    def _add(self, entry, meta):
        if entry.get("size", 0) > MAX_BYTES:
            raise Rejected("too large: %d MB, the limit is %d MB" % (entry["size"] >> 20, MAX_BYTES >> 20))
        work = tempfile.mkdtemp(dir=(os.makedirs(self.dir, exist_ok=True) or self.dir))
        try:
            path = os.path.join(work, entry["name"])
            self._blob(entry, path)
            try:
                self.app.check_sliced(path)
            except ValueError as e:
                raise Rejected(str(e))
            target, model = self._target(path, meta)
            problems = check_gcode_3mf(path, model)  # the guard that also runs at the print start
            if problems:
                raise Rejected(" ".join(problems))
            body = {"target": target, "model": model if target == "any" else "", "name": str(meta.get("name") or "")[:80] or None}
            for caller in self._callers(target, model, meta):
                try:
                    return self.app.queue_sliced(self.app.access.bind(self.app.queue, caller), path, body)
                except Forbidden:
                    continue  # this owner may not use that printer; the next candidate may
                except (ValueError, QueueError) as e:
                    raise Rejected(str(e))
            raise Rejected("no owner may add jobs for this printer")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _target(self, path, meta):
        """(target, model): job.json printer serial, else job.json model, else the model in the G-code header."""
        registry = self.app.registry
        if meta.get("printer"):
            p = registry.get(str(meta["printer"]))
            if not p:
                raise Rejected("unknown printer in job.json")
            return str(meta["printer"]), p["model"]
        want = str(meta.get("model") or file_model(path) or "")
        for p in registry.all():
            if want and same_model(p["model"], want):
                return "any", p["model"]
        raise Rejected("no registered printer of model '%s'" % want)

    def _callers(self, target, model, meta):
        """Who the job belongs to, first match: job.json owner (an HA user), the owner of the printer, then nobody (admin)."""
        out = []
        user = self.app.find_user(meta.get("owner")) if meta.get("owner") else None
        if user:
            out.append(Caller(user[0], user[1]))
        for p in self.app.registry.all():
            if p.get("owner") and (p["serial"] == target if target != "any" else same_model(p["model"], model)):
                u = self.app.get_user(p["owner"])
                out.append(Caller(p["owner"], (u or {}).get("name", "")))
                break
        out.append(Caller("", "", "Plates inbox", internal=True))
        return out
