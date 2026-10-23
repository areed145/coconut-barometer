"""The version in the JS and CSS addresses: /static/v/<version>/js/main.js and so on.

Those are cached for good (immutable): a new version is a new address, so a browser never mixes a
stale module with a fresh one. index.html isn't cached (it's always asked for again) and names the
current version; an address with any other version is a 404, so a tab left open across an update
fails to load a page cleanly (and offers a reload) instead of mixing versions. Images and the font
stay at their plain /static/ addresses, revalidated each time.

The version is a hash of the files' contents, worked out again only when one's size or modification
time changes, so it follows edits while the server runs (a stat per file per call).
"""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path

STATIC = Path(__file__).parent / "static"
VERSIONED = ("js", "css", "vendor")
_lock = threading.Lock()
_cache: dict = {"sig": None, "version": ""}


def version() -> str:
    files = sorted(p for d in VERSIONED for p in (STATIC / d).rglob("*") if p.is_file())
    sig = tuple((str(p), st.st_size, st.st_mtime_ns) for p in files for st in [p.stat()])
    with _lock:
        if sig != _cache["sig"]:
            h = hashlib.sha256()
            for p in files:
                h.update(str(p.relative_to(STATIC)).encode() + b"\0" + p.read_bytes())
            _cache.update(sig=sig, version=h.hexdigest()[:12])
        return _cache["version"]


def versioned(page: str) -> str:
    """index.html with its JS and CSS addresses pointing at the current version."""
    v = version()
    for d in VERSIONED:
        page = page.replace(f'"/static/{d}/', f'"/static/v/{v}/{d}/')
    return page
