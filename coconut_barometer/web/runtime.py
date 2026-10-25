"""What a running web app holds: the config, the Flickr client, the review worker (which
also runs syncs) and the scheduler that asks it to sync every web.sync_every_minutes.

All Flickr calls go through the one worker, in order, under the shared rate limit."""
from __future__ import annotations

import logging
import os
import secrets
import sys
import threading
import time

from .. import local
from ..alerts import CHECK_EVERY, Alerts
from ..errors import caught
from ..review import ReviewApp
from ..schedule import due
from ..weather.qc.sampler import QcService
from ..weather.radar import RadarService
from ..weather.service import WeatherService
from ..weather.snapshot import Snapshots
from ..aprs.service import AprsService
from ..store import Store
from ..visitors import Visitors
from .security import LoginThrottle

logger = logging.getLogger(__name__)


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
        self.radar = RadarService(cfg, self.db_path)
        self.snapshots = Snapshots(self.radar, self.db_path)   # Home's Radar card (#77)
        self.qc = QcService(cfg, self.radar)          # station QC's reference samples and checks (#105)
        self.alerts = Alerts(cfg, self.aprs.callsigns, self.radar, self.aprs.igate)
        self.visitors = Visitors(cfg, self.db_path)   # the site's own visitors (#83)
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
            logger.warning("No login yet. Open the app and finish setup with this code: %s", self.setup_code)

    def needs_setup(self) -> bool:
        return not self.store().usernames()

    def restart_soon(self, delay: float = 1.0) -> None:
        """Start again in place (same process, same arguments), after this response is sent:
        a staged restore.db.gz is restored on the way up. On Render the container keeps running."""
        def go():
            time.sleep(delay)
            logger.info("Restarting to restore the uploaded backup…")
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
        if local.on():                              # #92: no Flickr sync, no backups; a stand-in thread so it's once
            local.held("syncing with Flickr or making backups")
            self._scheduler = threading.Thread(target=lambda: None, name="scheduler (local mode)", daemon=True)
        else:
            self._scheduler = threading.Thread(target=self._schedule_loop, args=(first_delay,), name="scheduler",
                                               daemon=True)
        self._scheduler.start()
        # The weather stream (it waits for a token and for stations); each connect asks for a REST gap-fill.
        self.weather.start_stream(self._stop, on_connect=self.review.wx_sync)
        self.aprs.start_stream(self._stop)          # APRS-IS: positions of the callsigns in Settings → APRS
        self.radar.start(self._stop, self.store)     # MRMS and nationwide storm cells (weather/radar.py)
        if local.on():                              # station QC is the server's job: not sampled locally
            local.held("sampling for station QC")
        else:
            self.qc.start(self._stop, self.store)    # station QC (weather/qc/)
        threading.Thread(target=self._alerts_loop, name="alerts", daemon=True).start()
        threading.Thread(target=self.visitors.run, args=(self._stop, self.store), name="visitors", daemon=True).start()

    def _alerts_loop(self) -> None:
        """Settings → Alerts: what the streams stored, checked every minute (alerts.py)."""
        while not self._stop.wait(CHECK_EVERY):
            try:
                self.alerts.run_once(self.store())
            except Exception as e:
                caught(e, "alerts")

    def stop(self) -> None:
        self._stop.set()
        self.visitors.flush(self.store())           # the page views not written yet

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
                caught(e, "scheduler")
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
            caught(e, "backup")
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
        url = client.api.auth_url(perms="delete")
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
            ok = self.client is not None and bool(self.client.api.token_valid(perms="delete"))
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
