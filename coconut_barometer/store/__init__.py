"""Local DuckDB state: a cache of your library plus everything the tool has decided.

One connection, methods grouped by area:
  db.py          connection, SCHEMA and numbered upgrades (meta.schema_version), meta
  library.py     photos, albums, memberships: your library as last synced, plus local edits
  cache.py       classifications (vision results by photo + settings fingerprint), cameras (EXIF)
  queue.py       the review queue, decisions (applied / rejected / failed / undone) and applies to undo
  engagement.py  account totals, groups and pools, comments, faves
  stats.py       per-day stats from Flickr: photo_daily, account_daily, stats_days
  auth.py        the web app's users and sessions (session tokens stored hashed)
  gallery.py     photostream and album listings, photo detail
  analytics.py   daily series, gainers, breakdowns, per-photo timelines
  settings.py    admin-page settings, encrypted API keys, the change history
  weather.py     WeatherFlow stations and observations
  aprs.py        APRS positions heard for the tracked callsigns, and the packets the igate gated
  visitors.py    the site's own visitors: page views, link previews, and each day's totals once pruned

Tables
  photos           mirror of your photos (refreshed on every sync)
  albums           mirror of your albums
  memberships      which photo is in which album (refreshed on every sync)
  trips            runs of photos close in time and place, named (trips.py); trip_photos: which photo is in which
  classifications  cached vision results, keyed by photo + model/prompt fingerprint
  cameras          each photo's camera, lens and full EXIF (JSON), fetched once
  photo_stats      daily snapshot of each photo's views / faves / comments (one per sync day)
  account_stats    daily snapshot of your account's total views (flickr.stats.getTotalViews)
  flickr_groups    groups you belong to; group_photos: which of your photos are in each pool
  comments, faves  comments on your photos and who faved them (fetched only where counts changed)
  social_fetched   the count each photo had when its comments/faves were last fetched
  photo_daily      views / comments / faves per photo per day (Flickr stats, days in GMT)
  account_daily    the account's views per day, split by photos / photostream / albums ...
  stats_days       which days have been fetched completely ('done'), so far ('partial': today), or have no stats ('none')
  users, sessions  the web app's owner login; sessions by sha256 of their token
  queue            the photos in review and their proposed changes: new (waiting to be processed) |
                   pending (ready) | applying; applied or ignored photos leave it
  requested_jobs   jobs you asked for (re-process, undo, re-plan) not finished yet, queued again after a restart
  applied          each apply from review: what every change written replaced, so it can be undone
  decisions        per (photo, action, target): applied / rejected / failed / undone, for Analytics. It holds
                   nothing back from being proposed again, except that a location you passed on (rejected)
                   keeps the photo out of the daily sweep for photos without one
  settings         admin-page settings (path -> JSON), layered over config.yaml
  secrets          API keys, AES-GCM encrypted with a key kept outside the database
  settings_log     who changed which setting when (secrets masked)
  wx_stations, wx_devices, wx_obs, wx_events  WeatherFlow stations, their devices, 1-minute observations, strikes
  qc_rtma, qc_radar, qc_glm, qc_alerts  station QC's reference samples at each station (RTMA every 15 minutes,
                   radar every frame, GLM flashes within reach, NWS alerts covering it); qc_fronts: WPC's analyses
                   near the stations; qc_runs: each check's results, qc_flags: the periods they flagged (weather/qc/)
  aprs_positions   each position packet heard from a tracked callsign
  aprs_igate       each packet APRS-IS credits the igate with gating: who, where (if it said), heard direct or not
  site_hits        each page view of this site (no IPs: a visitor is a hash salted per day), for visitors.keep_days
  site_days        each day's visitors and page views, once its site_hits are pruned
  meta             key/values: schema_version, nsid, last_sync, licenses
"""
from __future__ import annotations

from .analytics import AnalyticsStore
from .alerts import AlertsStore
from .aprs import AprsStore
from .auth import AuthStore
from .cache import CacheStore
from .db import SCHEMA_VERSION, Database, SchemaTooNew
from .engagement import EngagementStore
from .gallery import GalleryStore
from .library import LibraryStore
from .qc import QcStore
from .queue import QueueStore
from .settings import SettingsStore
from .stats import StatsStore
from .visitors import VisitorsStore
from .weather import WeatherStore

__all__ = ["Store", "Database", "SCHEMA_VERSION", "SchemaTooNew"]


class Store(LibraryStore, CacheStore, QueueStore, EngagementStore, StatsStore, AuthStore, AlertsStore,
            GalleryStore, AnalyticsStore, SettingsStore, WeatherStore, QcStore, AprsStore, VisitorsStore):
    def counts(self) -> dict[str, int]:
        q = lambda sql: self.db.execute(sql).fetchone()[0]
        return {
            "photos": q("SELECT COUNT(*) FROM photos"),
            "geotagged": q("SELECT COUNT(*) FROM photos WHERE lat IS NOT NULL"),
            "albums": q("SELECT COUNT(*) FROM albums"),
            "classified": q("SELECT COUNT(DISTINCT photo_id) FROM classifications"),
            "cameras": q("SELECT COUNT(*) FROM cameras"),
            "to review": q("SELECT COUNT(*) FROM queue WHERE status='pending'"),
            "applied": q("SELECT COUNT(*) FROM decisions WHERE status='applied'"),
            "rejected": q("SELECT COUNT(*) FROM decisions WHERE status='rejected'"),
            "failed": q("SELECT COUNT(*) FROM decisions WHERE status='failed'"),
        }
