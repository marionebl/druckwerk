"""Content-Length of a request, shared by the bridge and the app (the bridge image gets its own copy)."""


def content_length(headers):
    """A missing header is 0. A negative or non-numeric value is a ValueError, which the servers answer with 400:
    read(-1) would otherwise read until the client closes the connection."""
    raw = (headers.get("Content-Length") or "0").strip()
    if not (raw.isascii() and raw.isdigit()):
        raise ValueError("Content-Length must be a non-negative integer.")
    return int(raw)
