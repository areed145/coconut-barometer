"""What a running web app holds: the config, the Flickr client, the review worker (which
also runs syncs) and the scheduler that asks it to sync every web.sync_every_minutes.

All Flickr calls go through the one worker, in order, under the shared rate limit."""
from __future__ import annotations

import threading
import time

from ..review import ReviewApp
from ..schedule import due
from ..store import Store
from .security import LoginThrottle


class Runtime:
    def __init__(self, cfg: dict, client, provider_factory=None, background: bool = True, process_kw=None):
        self.cfg = cfg
        self.db_path = cfg["database"]
        self.client = client
        self._local = threading.local()
        self.review = ReviewApp(self.store(), client, cfg=cfg, provider=provider_factory, background=background,
                                process_kw=process_kw)
        self.login_throttle = LoginThrottle()   # per app, so failed logins are counted per server
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self.next_sync: float | None = None

    def store(self) -> Store:
        """This thread's connection (sqlite connections belong to the thread that opened them)."""
        s = getattr(self._local, "store", None)
        if s is None:
            s = self._local.store = Store(self.db_path)
        return s

    # ---- scheduler ----------------------------------------------------------
    def start_scheduler(self, first_delay: float = 30) -> None:
        minutes = self.cfg["web"]["sync_every_minutes"]
        if minutes <= 0 or self._scheduler:
            return
        self._scheduler = threading.Thread(target=self._schedule_loop, args=(minutes * 60, first_delay),
                                           name="scheduler", daemon=True)
        self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()

    def _schedule_loop(self, every: float, first_delay: float) -> None:
        self.next_sync = time.time() + first_delay   # a first look shortly after startup
        while not self._stop.wait(max(0.0, self.next_sync - time.time())):
            try:
                self.review.sync()                 # whatever's due; ignored if a sync is already queued
            except Exception:
                pass
            self.next_sync = time.time() + every

    def sync_status(self) -> dict:
        import json
        store = self.store()
        meta = lambda k: int(store.get_meta(k) or 0) or None
        days = store.stats_days()
        result = store.get_meta("last_sync_result")
        return {
            "last_sync": meta("last_sync"), "last_full_sync": meta("last_full_sync"),
            "last_photo_list": meta("last_photo_list"), "last_incremental": meta("last_incremental"),
            "due": due(self.cfg, store), "next_run": self.next_sync, "running": self.review.syncing,
            "every_minutes": self.cfg["web"]["sync_every_minutes"], "schedule": self.cfg["schedule"],
            "stats_days": sum(1 for s in days.values() if s == "done"),
            "stats_complete": any(s == "none" for s in days.values()),
            "last_result": json.loads(result) if result else None,
        }
