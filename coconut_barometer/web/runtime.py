"""What a running web app holds: the config, the Flickr client, the review worker (which
also runs syncs) and the scheduler that asks it to sync every web.sync_every_minutes.

All Flickr calls go through the one worker, in order, under the shared rate limit."""
from __future__ import annotations

import os
import secrets
import sys
import threading
import time

from ..errors import reporter
from ..review import ReviewApp
from ..schedule import due
from ..weather.service import WeatherService
from ..aprs.service import AprsService
from ..store import Store
from .security import LoginThrottle


class Runtime:
    def __init__(self, cfg: dict, client, provider_factory=None, background: bool = True, process_kw=None,
                 cfg_path: str | None = None, client_factory=None):
        """cfg_path: where config.yaml is, so admin-page changes can be reloaded.
        client_factory(cfg): a new Flickr client when the API keys change."""
        self.cfg, self.cfg_path, self.client_factory = cfg, cfg_path, client_factory
        self.db_path = cfg["database"]
        self.client = client
        self._local = threading.local()
        self.review = ReviewApp(self.store(), client, cfg=cfg, provider=provider_factory, background=background,
                                process_kw=process_kw)
        self.weather = self.review.weather = WeatherService(cfg, self.db_path)
        self.aprs = AprsService(cfg, self.db_path)
        self.login_throttle = LoginThrottle()   # per app, so failed logins are counted per server
        self.flickr_pending: dict[str, tuple] = {}  # request token -> (client mid-login, started, username)
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self.next_sync: float | None = None
        self.restarter = lambda: os.execv(sys.executable, [sys.executable, *sys.argv])
        # No login yet: the setup page creates one, or restores a backup. Whoever reaches a
        # fresh server first could otherwise claim it, so it also asks for this code, which
        # only the server's log shows (Render: the service's Logs).
        self.setup_code = None
        if self.needs_setup():
            self.setup_code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
            print(f"No login yet. Open the app and finish setup with this code: {self.setup_code}", flush=True)

    def needs_setup(self) -> bool:
        return not self.store().usernames()

    def restart_soon(self, delay: float = 1.0) -> None:
        """Start again in place (same process, same arguments), after this response is sent:
        a staged restore.db.gz is restored on the way up. On Render the container keeps running."""
        def go():
            time.sleep(delay)
            print("Restarting to restore the uploaded backup…", flush=True)
            self.restarter()
        threading.Thread(target=go, name="restart", daemon=True).start()

    def store(self) -> Store:
        """This thread's connection (one connection per thread)."""
        s = getattr(self._local, "store", None)
        if s is None:
            s = self._local.store = Store(self.db_path)
        return s

    # ---- scheduler ----------------------------------------------------------
    def reload(self) -> dict:
        """Re-read every settings layer after an admin-page change, in place (everything
        holding self.cfg sees it). Rebuilds the vision provider / Flickr client if needed."""
        from ..config import load_config

        if not self.cfg_path:
            return {}
        new = load_config(self.cfg_path)
        old = self.cfg
        vision_changed = any(new["vision"][k] != old["vision"][k] for k in ("provider", "model", "api_key", "base_url"))
        keys_changed = (any(new["flickr"][k] != old["flickr"][k] for k in ("api_key", "api_secret"))
                        or self.client is None)
        self.cfg.clear()
        self.cfg.update(new)
        if vision_changed:
            self.review._provider = None          # made again, from the new settings, on next use
        if keys_changed and self.client_factory:
            self.client = self.review.client = self.client_factory(self.cfg)
        return {"vision": vision_changed, "flickr": keys_changed}

    def start_scheduler(self, first_delay: float = 30) -> None:
        if self._scheduler:
            return
        self._scheduler = threading.Thread(target=self._schedule_loop, args=(first_delay,), name="scheduler",
                                           daemon=True)
        self._scheduler.start()
        # The weather stream (it waits for a token and for stations); each connect asks for a REST gap-fill.
        self.weather.start_stream(self._stop, on_connect=self.review.wx_sync)
        self.aprs.start_stream(self._stop)          # APRS-IS: positions of the callsigns in Settings → APRS

    def stop(self) -> None:
        self._stop.set()

    def _schedule_loop(self, first_delay: float) -> None:
        """Reads web.sync_every_minutes each time round, so changing it on the admin page
        takes effect from the next run (0 pauses the scheduler)."""
        self.next_sync = time.time() + first_delay   # a first look shortly after startup
        while not self._stop.wait(max(0.0, self.next_sync - time.time())):
            self.backup_if_due()
            minutes = self.cfg["web"]["sync_every_minutes"]
            if minutes <= 0:
                self.next_sync = None
                self._stop.wait(60)
                self.next_sync = time.time()
                continue
            try:
                self.review.sync()                 # whatever's due; ignored if a sync is already queued
            except Exception as e:
                reporter.report(e, "scheduler")
            self.next_sync = time.time() + minutes * 60

    def backup_if_due(self) -> str | None:
        from ..backup import make_backup

        every = self.cfg["backup"]["every_hours"]
        store = self.store()
        if every <= 0 or time.time() - int(store.get_meta("last_backup") or 0) < every * 3600:
            return None
        try:
            path = make_backup(self.cfg)
        except Exception as e:   # e.g. disk full; try again next time round
            store.set_meta("last_backup_error", f"{int(time.time())}: {e}")
            return None
        store.set_meta("last_backup", int(time.time()))
        return path.name

    # ---- Flickr login from the admin page (OAuth 1.0a with a callback) ---------------
    FLICKR_LOGIN_TTL = 600

    def flickr_login_start(self, callback_url: str, username: str) -> str:
        """The flickr.com URL to approve access at. A separate client does the dance, so
        jobs using the current one aren't disturbed."""
        from urllib.parse import parse_qs, urlparse

        if not self.client_factory:
            raise RuntimeError("connecting Flickr isn't available in this session")
        if not (self.cfg["flickr"]["api_key"] and self.cfg["flickr"]["api_secret"]):
            raise ValueError("set the Flickr API key and secret first")
        now = time.time()
        for token, (_, started, _u) in list(self.flickr_pending.items()):
            if now - started > self.FLICKR_LOGIN_TTL:
                del self.flickr_pending[token]
        client = self.client_factory(self.cfg)
        client.api.get_request_token(oauth_callback=callback_url)
        url = client.api.auth_url(perms="write")
        token = parse_qs(urlparse(url).query)["oauth_token"][0]
        self.flickr_pending[token] = (client, now, username)
        return url

    def flickr_login_finish(self, token: str, verifier: str) -> str:
        """Exchange Flickr's verifier for a login, saved where the client reads it. Only a
        token this app issued (to a signed-in user, in the last 10 minutes) is accepted, once."""
        pending = self.flickr_pending.pop(token, None)
        if not pending or time.time() - pending[1] > self.FLICKR_LOGIN_TTL:
            raise PermissionError("unknown or expired Flickr login; start again from Settings")
        client = pending[0]
        client.api.get_access_token(verifier)
        self.client = self.review.client = self.client_factory(self.cfg)   # picks up the saved login
        return pending[2]

    def flickr_status(self) -> dict:
        try:
            ok = self.client is not None and bool(self.client.api.token_valid(perms="write"))
        except Exception:
            ok = False
        nsid = self.store().get_meta("nsid") if ok else None
        return {"connected": ok, "nsid": nsid, "keys_set": bool(self.cfg["flickr"]["api_key"] and self.cfg["flickr"]["api_secret"])}

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
