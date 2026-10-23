"""Per-day stats from Flickr (needs Pro): every photo viewed on a day with its views,
comments and faves that day, plus the account's daily totals.

A day costs 1 call for the totals + 1 per 100 photos viewed that day (typically 3-7).
Flickr's days run midnight to midnight GMT. Today is fetched on every run as it goes ("partial",
shown as "so far today"); a day is final once it has ended, and the last two are fetched again
later the same day in case Flickr was still counting.
Backfill walks back one day at a time until Flickr says it has no stats for a date
(around a year back), and never retries past that point.
"""
from __future__ import annotations

import calendar
import datetime as dt

from .flickr_client import FlickrCallError

NO_STATS = 2          # flickr.stats.* error 2: "No stats for that date"
SETTLE_HOURS = 12     # a just-finished day is re-fetched until a fetch this long after it ended
REFETCH_HOURS = 6     # ... but no more often than this


def fetch_day(client, store, day: str, partial: bool = False) -> int:
    """One day's stats, all pages. Returns photos viewed, or -1 if Flickr has none for it.
    partial: today, still going (none for it yet is not where the stats run out)."""
    try:
        totals = client.call("flickr.stats.getTotalViews", date=day)["stats"]
    except FlickrCallError as e:
        if e.code == NO_STATS:
            if not partial:
                store.mark_stats_day(day, "none")
            return -1
        raise
    flat = {k: int(v.get("views") or 0) for k, v in totals.items() if isinstance(v, dict)}
    photos, page = [], 1
    while True:
        r = client.call("flickr.stats.getPopularPhotos", date=day, sort="views", per_page=100, page=page)["photos"]
        photos += [{"photo_id": str(p["id"]), "views": int(p["stats"].get("views") or 0),
                    "comments": int(p["stats"].get("comments") or 0),
                    "favorites": int(p["stats"].get("favorites") or 0)} for p in r.get("photo", [])]
        if page >= int(r.get("pages") or 1):
            break
        page += 1
    store.save_stats_day(day, flat, photos, "partial" if partial else "done")
    return len(photos)


def days_due(store, max_backfill: int, now: float | None = None) -> list[str]:
    """Days to fetch now, newest first: recent days not yet final, then up to max_backfill
    older days not yet fetched, stopping where Flickr ran out of stats."""
    now = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    status = store.stats_days()
    fetched = store.stats_fetched_at()
    due, backfill = [], 0
    for back in range(1, 3660):
        day = (today - dt.timedelta(days=back)).isoformat()
        if status.get(day) == "none":
            break
        day_end = calendar.timegm((today - dt.timedelta(days=back - 1)).timetuple())   # next midnight GMT
        settled = fetched.get(day, 0) > day_end + SETTLE_HOURS * 3600
        if back <= 2 and not settled:
            if now - fetched.get(day, 0) > REFETCH_HOURS * 3600:
                due.append(day)
        elif status.get(day) in (None, "partial"):      # never fetched, or only while it was going
            if backfill >= max_backfill:
                break
            due.append(day)
            backfill += 1
    return due


def update(client, store, max_backfill: int = 7, log=print) -> dict:
    """Today so far, then the days that are due. Stops early (without error) where stats run out."""
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    if store.stats_days().get(today) != "none":
        fetch_day(client, store, today, partial=True)
    days = days_due(store, max_backfill)
    done = 0
    for day in days:
        n = fetch_day(client, store, day)
        if n < 0:
            log(f"  no stats before {day}; backfill complete")
            break
        done += 1
    return {"days": done, "due": len(days)}
