"""Local SQLite state: a cache of your library plus everything the tool has decided.

One connection, methods grouped by area:
  db.py          connection, SCHEMA and numbered upgrades (meta.schema_version), meta
  library.py     photos, albums, memberships: your library as last synced, plus local edits
  cache.py       classifications (vision results by photo + settings fingerprint), cameras (EXIF)
  queue.py       the review queue and decisions (applied / rejected / failed)
  engagement.py  account totals, groups and pools, comments, faves
  stats.py       per-day stats from Flickr: photo_daily, account_daily, stats_days

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
  stats_days       which days have been fetched completely ('done'), or have no stats ('none')
  queue            per-photo proposed changes for the review tool:
                   pending | applying | none (nothing to change) | applied | rejected | failed
  decisions        per (photo, action, target): applied / rejected / failed.
                   Rejected items are never proposed again.
  meta             key/values: schema_version, nsid, last_sync, licenses
"""
from __future__ import annotations

from .cache import CacheStore
from .db import SCHEMA_VERSION, Database
from .engagement import EngagementStore
from .library import LibraryStore
from .queue import QueueStore
from .stats import StatsStore

__all__ = ["Store", "Database", "SCHEMA_VERSION"]


class Store(LibraryStore, CacheStore, QueueStore, EngagementStore, StatsStore):
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
