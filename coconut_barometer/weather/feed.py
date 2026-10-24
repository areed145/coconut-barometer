"""A remote feed the Weather map shows (fronts, and the layers after them): fetched when the map asks and what's kept is
older than its TTL, only if it changed (If-Modified-Since / If-None-Match); while the source can't be reached, the last
data is kept (with the error). Each fetch is a call in activity.log.

A subclass gives `source`, `what` and `via` (for the activity log) and `load(a)`: fetch and parse, returning the new
data, or None when nothing changed (it may set a["detail"]). `conditional()` gives the headers for the last response
`remember()`ed, which load() calls once what it fetched has parsed.

A small fast feed is refreshed in the request that finds it stale (one at a time, under the lock). A big or slow one
(`background=True`) is refreshed in a thread instead: the request gets what's kept now, with `pending` while a
refresh is under way, so the map can ask again soon rather than wait."""
from __future__ import annotations

import threading
import time

import requests

from .. import activity
from ..errors import caught


def _thread(run) -> None:
    threading.Thread(target=run, daemon=True).start()


class Feed:
    source, what, via = "imagery", "", ""
    ttl = 15 * 60

    def __init__(self, get=requests.get, clock=time.time, ttl: int | None = None, background: bool = False, spawn=_thread):
        self._get, self._clock = get, clock
        self.ttl = self.ttl if ttl is None else ttl
        self._background, self._spawn = background, spawn
        self._lock = threading.RLock()
        self._validators: dict[str, str] = {}
        self._refreshing = False
        self.data = None
        self.checked_at: float | None = None
        self.error: str | None = None

    def load(self, a: dict):
        raise NotImplementedError

    def current(self) -> dict:
        """{data (None until the first fetch), checked_at, error, pending}: refreshed first if what's kept is stale."""
        start = False
        with self._lock:
            if not self._refreshing and (self.checked_at is None or self._clock() - self.checked_at >= self.ttl):
                if self._background:
                    self._refreshing = start = True
                    self.checked_at = self._clock()          # not asked again by every request while it runs
                else:
                    self._refresh()
            if not start:
                return self._state()
        self._spawn(self._run)
        with self._lock:
            return self._state()

    def _state(self) -> dict:
        return {"data": self.data, "checked_at": self.checked_at, "error": self.error, "pending": self._refreshing}

    def _run(self) -> None:
        try:
            self._refresh()
        finally:
            with self._lock:
                self._refreshing = False

    def _refresh(self) -> None:
        checked = self._clock()
        try:
            with activity.log.call(self.source, self.what, self.via) as a:
                data = self.load(a)
                if data is None:
                    a["status"] = "304"
            with self._lock:
                if data is not None:
                    self.data = data
                self.checked_at, self.error = checked, None
        except Exception as e:  # noqa: BLE001 - the source being down keeps the last data
            caught(e, self.what)
            with self._lock:
                self.checked_at, self.error = checked, f"{type(e).__name__}: {e}"[:200]

    # ---- conditional requests ---------------------------------------------------------------------------------
    def conditional(self) -> dict:
        """The headers that ask for the last remembered response only if it has changed since."""
        h = {}
        if "Last-Modified" in self._validators:
            h["If-Modified-Since"] = self._validators["Last-Modified"]
        if "ETag" in self._validators:
            h["If-None-Match"] = self._validators["ETag"]
        return h

    def remember(self, r) -> None:
        self._validators = {k: r.headers[k] for k in ("Last-Modified", "ETag") if r.headers.get(k)}
