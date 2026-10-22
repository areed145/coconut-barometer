"""A rate limit shared by every process using the same Flickr API key.

Flickr's limit (3600 calls/hour) is per API key, but review, process and social are
separate processes. Each call reserves the next free slot in a small file under
~/.flickr/ (one per key) while holding a lock on it, then sleeps until that slot.
So however many commands run at once, together they make at most calls_per_second.

Times are wall-clock (time.time), since they're compared across processes. Where file
locking isn't available (Windows), the limit applies within each process only.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time

try:
    import fcntl
except ImportError:   # Windows
    fcntl = None

STALE_AFTER = 3600   # a reservation this far ahead (e.g. the clock was changed) is reset


def limiter_path(api_key: str, directory: str = "~/.flickr") -> str:
    """Where the shared reservation lives for this API key (the key itself isn't stored)."""
    digest = hashlib.sha1((api_key or "").encode()).hexdigest()[:12]
    return os.path.join(os.path.expanduser(directory), f"rate-{digest}.lock")


class SharedRateLimiter:
    def __init__(self, path: str, calls_per_second: float):
        self.path = path
        self.interval = 1.0 / calls_per_second if calls_per_second > 0 else 0.0
        self._local = threading.Lock()   # threads in this process (the review worker + the page)
        self._last = 0.0                 # used only without fcntl
        if fcntl:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def wait(self, on_slot=None) -> float:
        """Block until this caller may make one call. Returns how long it waited.
        on_slot(slot): told the time it was given, before the wait (the Queue page shows it)."""
        if self.interval <= 0:
            return 0.0
        with self._local:
            slot = self._reserve()
        if on_slot:
            on_slot(slot)
        delay = slot - time.time()
        if delay > 0:
            time.sleep(delay)
        return max(0.0, delay)

    def next_free(self) -> float:
        """When the next call could go, by any process using this key (without reserving it)."""
        if self.interval <= 0:
            return 0.0
        if not fcntl:
            return max(time.time(), self._last + self.interval)
        try:
            with open(self.path) as f:
                return max(time.time(), float(f.read().strip() or 0))
        except (OSError, ValueError):
            return time.time()

    def _reserve(self) -> float:
        now = time.time()
        if not fcntl:
            slot = max(now, self._last + self.interval)
            self._last = slot
            return slot
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                try:
                    next_free = float(f.read().strip() or 0)
                except ValueError:
                    next_free = 0.0
                if next_free > now + STALE_AFTER:
                    next_free = 0.0
                slot = max(now, next_free)
                f.seek(0)
                f.truncate()
                f.write(repr(slot + self.interval))
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return slot
