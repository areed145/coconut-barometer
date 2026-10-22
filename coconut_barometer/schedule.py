"""What's due, and doing it: the one entry point for keeping the database in step with
Flickr. `sync` runs it; the web app's scheduler will call it hourly.

  full sync     every schedule.full_every_days (or when there's never been one)
  photo list    every schedule.photo_list_every_hours: view counts + the daily snapshot
  incremental   otherwise: recently updated photos, changed albums, new comments/faves
  stats         yesterday's per-day stats (once final) + schedule.backfill_days_per_run
                older days, until Flickr has none
"""
from __future__ import annotations

import time

from . import stats_history
from .config import VISION_THUMBNAIL
from .incremental import sync_incremental, sync_photo_list
from .sync import sync as full_sync


def due(cfg: dict, store, now: float | None = None) -> list[str]:
    now = now if now is not None else time.time()
    sc = cfg["schedule"]
    meta = lambda k: int(store.get_meta(k) or 0)
    if now - meta("last_full_sync") > sc["full_every_days"] * 86400:
        return ["full"]
    if now - meta("last_photo_list") > sc["photo_list_every_hours"] * 3600:
        return ["photo_list", "incremental"]
    return ["incremental"]


def run_due(cfg: dict, client, store, force: str | None = None, stats: bool | None = None, log=print) -> dict:
    """force: "full" | "photo_list" | "incremental" to run that instead of what's due.
    stats: override schedule.stats (False for quick syncs before process/social)."""
    thumb = VISION_THUMBNAIL
    started_calls = client.call_count
    steps = {"full": ["full"], "photo_list": ["photo_list", "incremental"],
             "incremental": ["incremental"]}[force] if force else due(cfg, store)
    out: dict = {"ran": steps}
    if "full" in steps:
        log("Full sync…")
        out["full"] = full_sync(client, store, thumb, log=log)
    if "photo_list" in steps:
        log("Re-reading every photo (counts, daily snapshot)…")
        out["photo_list"] = sync_photo_list(client, store, thumb, log)
    if "incremental" in steps:
        log("Incremental sync…")
        out["incremental"] = sync_incremental(client, store, thumb, photos="photo_list" not in steps, log=log)
    if cfg["schedule"]["stats"] if stats is None else stats:
        try:
            out["stats"] = stats_history.update(client, store, cfg["schedule"]["backfill_days_per_run"], log)
        except Exception as e:   # stats need Flickr Pro; never let them break a sync
            out["stats"] = {"error": str(e)}
            log(f"  ! stats: {e}")
    out["calls"] = client.call_count - started_calls
    store.set_meta("last_sync", int(time.time()))
    return out
