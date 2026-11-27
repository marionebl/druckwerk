"""Print files: the list under /media/druckwerk, path checks, and the checks of an upload. All blocking."""
import hashlib
import os
import re
import tempfile
import zipfile

SUFFIX = ".gcode.3mf"
MAX_UPLOAD = 64 * 1024 * 1024
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,118}\.gcode\.3mf")
PLATE = re.compile(r"Metadata/plate_\d+\.gcode")


def scan(base):
    """Relative POSIX paths of all *.gcode.3mf below base, sorted."""
    found = []
    for dirpath, _dirs, names in os.walk(base):
        for name in names:
            if name.lower().endswith(SUFFIX):
                found.append(os.path.relpath(os.path.join(dirpath, name), base).replace(os.sep, "/"))
    return sorted(found)


def _inside(path, root):
    return os.path.commonpath([path, root]) == root


def resolve(file, base, roots):
    """The real path of a file the caller named. ValueError with the text the integration showed.

    A relative name must stay inside base, an absolute path inside one of roots. realpath defeats ../ and symlinks.
    """
    base = os.path.realpath(base)
    if os.path.isabs(file):
        real = os.path.realpath(file)
        if not any(_inside(real, os.path.realpath(r)) for r in roots):
            raise ValueError("Path %s is outside the allowed folders (/media, /share)" % file)
    else:
        real = os.path.realpath(os.path.join(base, file))
        if not _inside(real, base):
            raise ValueError("Path %s is outside the druckwerk media folder" % file)
    if not real.lower().endswith(SUFFIX):
        raise ValueError("Only %s files can be printed" % SUFFIX)
    if not os.path.isfile(real):
        raise ValueError("File not found: %s" % file)
    return real


def read(path):
    with open(path, "rb") as f:
        return f.read()


class UploadError(Exception):
    def __init__(self, status, text):
        super().__init__(text)
        self.status = status


def store(base, name, src, length, overwrite):
    """Copy length bytes from the stream src into base/name after checking them. Returns {file, bytes, sha256}."""
    if not NAME.fullmatch(name):
        raise UploadError(400, "Name must be letters, digits, '._ -' and end in .gcode.3mf.")
    if length <= 0:
        raise UploadError(411, "Content-Length is required.")
    if length > MAX_UPLOAD:
        raise UploadError(413, "File too large (limit %d bytes)." % MAX_UPLOAD)
    os.makedirs(base, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=base, suffix=".part")  # same folder, so the final rename is atomic
    try:
        digest, left = hashlib.sha256(), length
        with os.fdopen(fd, "wb") as out:
            while left:
                chunk = src.read(min(left, 1 << 16))
                if not chunk:
                    raise UploadError(400, "The upload was cut short.")
                digest.update(chunk)
                out.write(chunk)
                left -= len(chunk)
        try:
            with zipfile.ZipFile(tmp) as z:
                plates = [n for n in z.namelist() if PLATE.fullmatch(n)]
        except zipfile.BadZipFile:
            raise UploadError(400, "Body is not a 3MF (zip) file.")
        if not plates:
            raise UploadError(400, "The 3MF has no sliced plate (Metadata/plate_N.gcode); export it as .gcode.3mf.")
        dest = os.path.join(base, name)
        if os.path.exists(dest) and not overwrite:
            raise UploadError(409, "File exists; add ?overwrite=1 to replace it.")
        os.replace(tmp, dest)
        os.chmod(dest, 0o644)
        return {"file": name, "bytes": length, "sha256": digest.hexdigest()}
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
