"""Local DuckDB state: a cache of your library plus everything the tool has decided.

One connection, methods grouped by area:
  db.py          connection, SCHEMA and numbered upgrades (meta.schema_version), meta
  library.py     photos, albums, memberships: your library as last synced, plus local edits
  cache.py       classifications (vision results by photo + settings fingerprint), cameras (EXIF)
  queue.py       the review queue and decisions (applied / rejected / failed)
  engagement.py  account totals, groups and pools, comments, faves
  stats.py       per-day stats from Flickr: photo_daily, account_daily, stats_days
  auth.py        the web app's users and sessions (session tokens stored hashed)
  gallery.py     photostream and album listings, photo detail
  analytics.py   daily series, gainers, breakdowns, per-photo timelines
  settings.py    admin-page settings, encrypted API keys, the change history
  weather.py     WeatherFlow stations and observations
  aprs.py        APRS positions heard for the tracked callsigns

Tables
  photos           mirror of your photos (refreshed on every sync)
  albums           mirror of your albums
  memberships      which photo is in which album (refreshed on every sync)
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
  queue            per-photo proposed changes for the review tool:
                   pending | applying | none (nothing to change) | applied | rejected | failed
  applied          each apply from review: what every change written replaced, so it can be undone
  decisions        per (photo, action, target): applied / rejected / failed / undone.
                   Rejected items are never proposed again.
  settings         admin-page settings (path -> JSON), layered over config.yaml
  secrets          API keys, AES-GCM encrypted with a key kept outside the database
  settings_log     who changed which setting when (secrets masked)
  wx_stations, wx_devices, wx_obs, wx_events  WeatherFlow stations, their devices, 1-minute observations, strikes
  aprs_positions   each position packet heard from a tracked callsign
  meta             key/values: schema_version, nsid, last_sync, licenses
"""
from __future__ import annotations

from .analytics import AnalyticsStore
from .aprs import AprsStore
from .auth import AuthStore
from .cache import CacheStore
from .db import SCHEMA_VERSION, Database, SchemaTooNew
from .engagement import EngagementStore
from .gallery import GalleryStore
from .library import LibraryStore
from .queue import QueueStore
from .settings import SettingsStore
from .stats import StatsStore
from .weather import WeatherStore

__all__ = ["Store", "Database", "SCHEMA_VERSION", "SchemaTooNew"]


class Store(LibraryStore, CacheStore, QueueStore, EngagementStore, StatsStore, AuthStore,
            GalleryStore, AnalyticsStore, SettingsStore, WeatherStore, AprsStore):
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
