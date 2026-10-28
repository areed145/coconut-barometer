"""A rate limit shared by every process using the same Flickr API key.

Flickr's limit (3600 calls/hour) is per API key and counted over the hour, not per second,
so calls go as fast as calls_per_second allows until the last hour holds calls_per_hour of
them, and then each waits for the oldest to age out. Review, process and social are separate
processes: each call reserves its slot in a small file under ~/.flickr/ (one per key, the
slots of the last hour, one per line) while holding a lock on it, then sleeps until that slot.
So however many commands run at once, together they stay within both limits.

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

WINDOW = 3600        # Flickr counts calls over an hour
STALE_AFTER = 7200   # a reservation this far ahead (e.g. the clock was changed) resets them all


def limiter_path(api_key: str, directory: str = "~/.flickr") -> str:
    """Where the shared reservations live for this API key (the key itself isn't stored)."""
    digest = hashlib.sha1((api_key or "").encode()).hexdigest()[:12]
    return os.path.join(os.path.expanduser(directory), f"rate-{digest}.lock")


def estimate_seconds(calls: int, calls_per_hour: int, calls_per_second: float) -> float:
    """Roughly how long `calls` take from a fresh hour: the hour's budget at full speed, the rest
    at the hourly rate (for "~N min" before a long command; ignores calls already made)."""
    fast = min(calls, calls_per_hour) if calls_per_hour > 0 else calls
    seconds = fast / calls_per_second if calls_per_second > 0 else 0.0
    if calls_per_hour > 0 and calls > calls_per_hour:
        seconds = max(seconds, (calls - calls_per_hour) * WINDOW / calls_per_hour)
    return seconds


class SharedRateLimiter:
    def __init__(self, path: str, calls_per_hour: int, calls_per_second: float = 0.0, window: float = WINDOW):
        """calls_per_hour: at most this many in any `window` seconds; calls_per_second: and at most
        this often. 0 turns either off."""
        self.path = path
        self.per_window = max(0, int(calls_per_hour))
        self.window = window
        self.interval = 1.0 / calls_per_second if calls_per_second > 0 else 0.0
        self._local = threading.Lock()   # threads in this process (the review worker + the page)
        self._slots: list[float] = []    # used only without fcntl
        if fcntl:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    @property
    def unlimited(self) -> bool:
        return not self.per_window and not self.interval

    def wait(self, on_slot=None) -> float:
        """Block until this caller may make one call. Returns how long it waited.
        on_slot(slot): told the time it was given, before the wait (the Status page shows it)."""
        if self.unlimited:
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
        if self.unlimited:
            return 0.0
        now = time.time()
        if not fcntl:
            return self._next_slot(self._slots, now)
        try:
            with open(self.path) as f:
                return self._next_slot(self._prune(self._parse(f.read()), now), now)
        except OSError:
            return now

    # ---- slots ------------------------------------------------------------
    @staticmethod
    def _parse(text: str) -> list[float]:
        try:
            return sorted(float(line) for line in text.split())
        except ValueError:
            return []

    def _prune(self, slots: list[float], now: float) -> list[float]:
        """Drop slots that can't hold up a call any more, or all of them if one is impossibly far ahead."""
        if slots and slots[-1] > now + STALE_AFTER:
            return []
        if not self.per_window:
            return slots[-1:]
        return [s for s in slots if s > now - self.window]

    def _next_slot(self, slots: list[float], now: float) -> float:
        slot = now
        if slots and self.interval:
            slot = max(slot, slots[-1] + self.interval)
        if self.per_window and len(slots) >= self.per_window:
            slot = max(slot, slots[-self.per_window] + self.window)
        return slot

    def _reserve(self) -> float:
        now = time.time()
        if not fcntl:
            self._slots = self._prune(self._slots, now)
            slot = self._next_slot(self._slots, now)
            self._slots.append(slot)
            return slot
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                slots = self._prune(self._parse(f.read()), now)
                slot = self._next_slot(slots, now)
                slots.append(slot)
                f.seek(0)
                f.truncate()
                f.write("\n".join(map(repr, slots)))
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return slot
