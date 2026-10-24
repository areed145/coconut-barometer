"""Everything the app asks of the outside world, for the Status page: each call (who, what, how long it
waited for its rate limit, how long it took, what came back), each stream message, each finished job,
and the calls in flight or waiting for a rate-limit slot right now.

There's one per process, `log` below, because the clients that call out are made in many places. Nothing
is stored: it starts empty with the app. Each source keeps its own newest entries, so a chatty stream
can't push the Flickr calls out.

Sources: flickr (API), cdn (photo downloads for the vision model), vision, weatherflow (REST),
wx-stream (WeatherFlow WebSocket), aprs (APRS-IS), imagery (radar / satellite frame lists), radar (MRMS frames
and storm cells near the stations), geocode (the reviewer's
place search, OpenStreetMap's Nominatim), jobs (the worker)."""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from contextlib import contextmanager

KEEP = 300              # newest entries per source
HOUR = 3600


class Activity:
    def __init__(self, keep: int = KEEP):
        self.lock = threading.Lock()
        self._seq = itertools.count(1)
        self._keep = keep
        self.entries: dict[str, deque] = {}
        self.pending: dict[int, dict] = {}            # in flight, or waiting for a slot: token -> entry
        self.totals: dict[str, dict] = {}             # per source since start: calls, errors, waited (s), skipped
        self._times: dict[str, deque] = {}            # per source: when each call started, for the last hour

    def _source(self, source: str) -> None:
        if source not in self.entries:
            self.entries[source] = deque(maxlen=self._keep)
            self.totals[source] = {"calls": 0, "errors": 0, "waited": 0.0, "skipped": 0}
            self._times[source] = deque(maxlen=20000)

    # ---- calls ----------------------------------------------------------------------
    def begin(self, source: str, what: str, detail: str = "", until: float | None = None) -> int:
        """A call about to be made. `until`: it waits for a rate-limit slot until then. Returns a token."""
        now = time.time()
        with self.lock:
            self._source(source)
            token = next(self._seq)
            waiting = until is not None and until > now
            self.pending[token] = {"seq": token, "source": source, "what": what, "detail": detail, "queued": now,
                                   "at": None if waiting else now, "until": until if waiting else None,
                                   "state": "waiting" if waiting else "running"}
            return token

    def until(self, token: int, slot: float) -> None:
        """The rate limiter gave this call a slot: it waits until then."""
        with self.lock:
            p = self.pending.get(token)
            if p and slot > time.time():
                p.update(state="waiting", until=slot, at=None)

    def running(self, token: int) -> None:
        with self.lock:
            p = self.pending.get(token)
            if p and p["state"] != "running":
                p.update(state="running", at=time.time(), until=None)

    def end(self, token: int, status: str = "ok", detail: str | None = None, error: bool = False) -> None:
        now = time.time()
        with self.lock:
            p = self.pending.pop(token, None)
            if not p:
                return
            at = p["at"] or now
            entry = {"seq": next(self._seq), "source": p["source"], "kind": "call", "what": p["what"],
                     "detail": p["detail"] if detail is None else detail, "status": status, "error": error,
                     "at": at, "ms": int((now - at) * 1000), "waited": round(at - p["queued"], 2)}
            t = self.totals[p["source"]]
            t["calls"] += 1
            t["errors"] += error
            t["waited"] += entry["waited"]
            self._times[p["source"]].append(at)
            self.entries[p["source"]].append(entry)

    @contextmanager
    def call(self, source: str, what: str, detail: str = ""):
        """A call with no rate limit of its own. The block may set ["status"] / ["detail"] on what it's given;
        an exception is recorded as an error and goes on up."""
        token, result = self.begin(source, what, detail), {}
        self.running(token)
        try:
            yield result
        except Exception as e:
            self.end(token, "error", f"{type(e).__name__}: {e}"[:300], error=True)
            raise
        self.end(token, result.get("status", "ok"), result.get("detail"), result.get("error", False))

    def latest(self, source: str) -> dict:
        """For the status panel: when this source's last call finished, its last success, and the error if the
        newest call failed. All None until it has made one."""
        with self.lock:
            calls = [e for e in self.entries.get(source, ()) if e["kind"] == "call"]
        newest = calls[-1] if calls else None
        ok = next((e for e in reversed(calls) if not e["error"]), None)
        return {"at": newest and newest["at"], "ok_at": ok and ok["at"],
                "error": newest["detail"] if newest and newest["error"] else None}

    # ---- stream messages, connections, job results ----------------------------------
    def record(self, source: str, what: str, detail: str = "", status: str = "ok", error: bool = False,
               kind: str = "message", ms: int | None = None) -> None:
        with self.lock:
            self._source(source)
            self.entries[source].append({"seq": next(self._seq), "source": source, "kind": kind, "what": what,
                                         "detail": detail, "status": status, "error": error, "at": time.time(),
                                         "ms": ms, "waited": None})
            self.totals[source]["errors"] += error

    def skip(self, source: str) -> None:
        """A message counted but not listed (WeatherFlow's 3-second wind, APRS-IS keepalives)."""
        with self.lock:
            self._source(source)
            self.totals[source]["skipped"] += 1

    # ---- reading --------------------------------------------------------------------
    def since(self, seq: int = 0, limit: int = 500) -> list[dict]:
        """Entries after `seq`, oldest first (at most the newest `limit`)."""
        with self.lock:
            out = [e for q in self.entries.values() for e in q if e["seq"] > seq]
        return sorted(out, key=lambda e: e["seq"])[-limit:]

    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            sources = {}
            for s, t in self.totals.items():
                times = self._times[s]
                sources[s] = {**t, "waited": round(t["waited"], 1),
                              "last_minute": sum(1 for x in times if x > now - 60),
                              "last_hour": sum(1 for x in times if x > now - HOUR),
                              "last_at": self.entries[s][-1]["at"] if self.entries[s] else None}
            pending = sorted((dict(p) for p in self.pending.values()), key=lambda p: p["seq"])
        return {"now": now, "sources": sources, "pending": pending}


log = Activity()
