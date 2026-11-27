"""Fetch a sliced plate from the private plates repo on GitHub, for the action add_to_queue (field file_url).

Safe by design: only the GitHub contents API URL of the repo in the option plates_repo is fetched, with the token of the
option plates_token, no redirect is followed, the size and the time are capped and the SHA-256 must match. All blocking.
The token only goes into the Authorization header. No error text and no log line carries it.
"""
import hashlib
import http.client
import os
import re
import secrets
import tempfile
import time
import zipfile
from urllib.parse import parse_qsl, unquote, urlsplit

import files

DEFAULT_REPO = "marionebl/druckwerk-plates"
API_BASE = "https://api.github.com"  # tests give the fake server's base to fetch(); a caller cannot
MAX_BYTES = files.MAX_UPLOAD  # 64 MB
TOTAL_TIMEOUT = 120  # seconds for the whole download
SOCKET_TIMEOUT = 30
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
REF = re.compile(r"[A-Za-z0-9._/-]{1,100}")
TOKEN = re.compile(r"[\x21-\x7e]{1,500}")
SHA256 = re.compile(r"[0-9a-fA-F]{64}")
TOKEN_HELP = "Set plates_token in the app options (a read-only GitHub token for the plates repo), then try again."


class PlateError(Exception):
    """A refusal or failure with the HTTP status the route answers. The text never holds the token."""

    def __init__(self, status, text):
        super().__init__(text)
        self.status = status


def check_sha256(value):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise PlateError(400, "sha256 must be 64 hex characters.")
    return value.lower()


def parse_url(url, repo, base=API_BASE):
    """(host, port, tls, request target, file name) of an allowed URL, or PlateError. Nothing is fetched here."""
    if not REPO.fullmatch(repo or ""):
        raise PlateError(400, "The app option plates_repo must look like owner/name.")
    want = urlsplit(base)
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        raise PlateError(400, "file_url is not a valid URL.")
    shown = "https://api.github.com/repos/%s/contents/<path>?ref=<branch>" % repo
    if (u.scheme != want.scheme or u.hostname != want.hostname or port != want.port or u.username is not None
            or u.password is not None or "@" in u.netloc or u.fragment):
        raise PlateError(400, "file_url must be %s (no other host, scheme, port or user)." % shown)
    prefix = "/repos/%s/contents/" % repo
    if not u.path.lower().startswith(prefix.lower()) or len(u.path) == len(prefix):
        raise PlateError(400, "file_url must be %s (repo %s only)." % (shown, repo))
    parts = unquote(u.path[len(prefix):]).split("/")
    if any(p in ("", ".", "..") or "\\" in p or "\x00" in p for p in parts):
        raise PlateError(400, "file_url has a bad path.")
    query = parse_qsl(u.query, keep_blank_values=True)
    if len(query) != 1 or query[0][0] != "ref" or not REF.fullmatch(query[0][1]) or ".." in query[0][1]:
        raise PlateError(400, "file_url needs exactly the query ?ref=<branch>.")
    name = parts[-1]
    if not name.lower().endswith(files.SUFFIX):
        raise PlateError(400, "The plate must be a %s file." % files.SUFFIX)
    return u.hostname, port, u.scheme == "https", u.path + "?" + u.query, name


def safe_name(name):
    """A file name for the media folder: no path parts, only the characters of an upload name, ends in .gcode.3mf."""
    stem = name[:-len(files.SUFFIX)]
    stem = re.sub(r"[^A-Za-z0-9._ -]", "_", stem).strip(" .") or "plate"
    if not stem[0].isalnum():
        stem = "p" + stem
    stem = stem[:100].replace("..", "_")
    out = stem + files.SUFFIX
    if not files.NAME.fullmatch(out):
        raise PlateError(400, "The plate name is not usable.")
    return out


def _place(tmp, base, name):
    """Move tmp to base/name; a name that exists gets a short unique suffix. Never overwrites."""
    stem = name[:-len(files.SUFFIX)]
    for attempt in range(20):
        dest = os.path.join(base, name if attempt == 0 else "%s_%s%s" % (stem, secrets.token_hex(3), files.SUFFIX))
        try:
            os.link(tmp, dest)  # fails when dest exists: no silent overwrite, no race
        except FileExistsError:
            continue
        os.chmod(dest, 0o644)
        return dest
    raise PlateError(500, "Could not find a free file name.")


def fetch(url, sha256, base, repo, token, api_base=API_BASE, clock=time.monotonic, log=lambda msg: None):
    """Download the plate into the folder base. Returns the stored path. Raises PlateError.

    api_base is for tests only: it is a parameter of this function and no request field reaches it.
    """
    repo = (repo or "").strip() or DEFAULT_REPO
    sha256 = check_sha256(sha256)
    host, port, tls, target, name = parse_url(url, repo, api_base)
    name = safe_name(name)
    token = (token or "").strip()
    if not token:
        raise PlateError(400, TOKEN_HELP)
    if not TOKEN.fullmatch(token):
        raise PlateError(400, "plates_token has characters that a GitHub token does not have. Enter it again in the app options.")
    os.makedirs(base, exist_ok=True)
    deadline = clock() + TOTAL_TIMEOUT
    cls = http.client.HTTPSConnection if tls else http.client.HTTPConnection
    conn = cls(host, port, timeout=SOCKET_TIMEOUT)
    fd, tmp = tempfile.mkstemp(dir=base, suffix=".part")
    try:
        try:
            conn.request("GET", target, headers={
                "Authorization": "Bearer " + token, "Accept": "application/vnd.github.raw", "User-Agent": "druckwerk-app",
                "X-GitHub-Api-Version": "2022-11-28"})
            resp = conn.getresponse()
        except Exception as e:  # noqa: BLE001 - only the type goes out: some messages quote request headers
            raise PlateError(502, "GitHub could not be reached (%s)." % type(e).__name__)
        if resp.status in (301, 302, 303, 307, 308):
            raise PlateError(502, "GitHub answered with a redirect; the app follows none.")
        if resp.status in (401, 403):
            raise PlateError(502, "GitHub refused the token (HTTP %d). Check plates_token and its access to %s." % (resp.status, repo))
        if resp.status == 404:
            raise PlateError(502, "GitHub found no such file in %s (HTTP 404). Check the path and the branch, and that plates_token may read the repo." % repo)
        if resp.status != 200:
            raise PlateError(502, "GitHub answered HTTP %d." % resp.status)
        length = resp.getheader("Content-Length")
        if length and length.isdigit() and int(length) > MAX_BYTES:
            raise PlateError(413, "The plate is larger than %d MB." % (MAX_BYTES >> 20))
        digest, total = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as out:
                fd = None
                while True:
                    if clock() > deadline:
                        raise PlateError(504, "The download took longer than %d s." % TOTAL_TIMEOUT)
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise PlateError(413, "The plate is larger than %d MB." % (MAX_BYTES >> 20))
                    digest.update(chunk)
                    out.write(chunk)
        except (OSError, http.client.HTTPException) as e:
            raise PlateError(502, "The download broke off (%s)." % type(e).__name__)
        if digest.hexdigest() != sha256:
            raise PlateError(400, "SHA-256 mismatch: the file has %s, the call named %s. Nothing was stored." % (digest.hexdigest(), sha256))
        try:
            with zipfile.ZipFile(tmp) as z:
                sliced = any(files.PLATE.fullmatch(n) for n in z.namelist())
        except zipfile.BadZipFile:
            raise PlateError(400, "The file is not a 3MF (zip) file.")
        if not sliced:
            raise PlateError(400, "The 3MF has no sliced plate (Metadata/plate_N.gcode); export it as .gcode.3mf.")
        dest = _place(tmp, base, name)
        log("plate stored: %s (%d bytes)" % (os.path.basename(dest), total))
        return dest
    finally:
        conn.close()
        if fd is not None:
            os.close(fd)
        if os.path.exists(tmp):
            os.remove(tmp)
