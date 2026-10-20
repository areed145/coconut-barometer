"""Cheap syncs between full ones: only what changed since last time.

  photos     flickr.photos.recentlyUpdated since the last run (new + edited), usually 1 call
  deletions  your photo count from flickr.people.getInfo; if it doesn't match the local
             count, the whole photo list is re-read (~1 call per 500 photos) to find them
  albums     the album list (1 call) carries each album's count and last update; only
             albums whose count or update changed have their photos re-read
  activity   flickr.activity.userPhotos for the time since the last run: photos with new
             comments or faves get their counts updated and their comments/faves re-read

Typically 4-6 calls an hour. sync_photo_list (the daily read of every photo, which is what
keeps view counts and the daily snapshot current) and the weekly full sync are in schedule.py.
"""
from __future__ import annotations

import math
import time

from . import social
from .flickr_client import PHOTO_EXTRAS, paginate
from .sync import _keep_changes_since, fetch_album_members, fetch_albums, fetch_photo_list, normalize_photo

MARGIN = 600   # seconds of overlap with the previous run, so nothing falls between two runs


def sync_photo_list(client, store, thumb_key: str = "url_m", log=print) -> dict:
    """Every photo re-read: counts current, deletions removed, today's snapshot taken."""
    started = int(time.time())
    photos = fetch_photo_list(client, thumb_key, log)
    _keep_changes_since(store, started, photos, [])
    gone = store.photo_ids() - {p["id"] for p in photos}
    store.upsert_photos(photos)
    store.delete_photos(gone)
    store.snapshot_counts()
    store.set_meta("last_photo_list", started)
    return {"photos": len(photos), "deleted": len(gone)}


def sync_incremental(client, store, thumb_key: str = "url_m", photos: bool = True, log=print) -> dict:
    """photos=False skips the recently-updated and deletion checks (when the whole photo
    list was just read anyway)."""
    started = int(time.time())
    since = int(store.get_meta("last_incremental") or store.get_meta("last_sync") or 0) - MARGIN
    nsid = store.get_meta("nsid") or client.nsid()
    out: dict = {}

    if photos:
        changed = [normalize_photo(p, thumb_key) for p in paginate(
            client, "flickr.photos.recentlyUpdated", "photos", "photo", min_date=max(since, 0), extras=PHOTO_EXTRAS)]
        _keep_changes_since(store, started, changed, [])
        new = len({p["id"] for p in changed} - store.photo_ids())
        store.upsert_photos(changed)
        out.update(updated=len(changed) - new, new=new)
        count = int(client.call("flickr.people.getInfo", user_id=nsid)["person"]["photos"]["count"]["_content"])
        if count != len(store.photo_ids()):
            log(f"  photo count differs ({count} on Flickr, {len(store.photo_ids())} here): re-reading the list")
            out["deleted"] = sync_photo_list(client, store, thumb_key, log)["deleted"]

    have = {a["id"]: a for a in store.albums()}
    albums = fetch_albums(client, nsid)
    changed_albums = [a for a in albums if a["id"] not in have or have[a["id"]]["count"] != a["count"]
                      or have[a["id"]]["date_update"] != a["date_update"]]
    for a in changed_albums:
        members = fetch_album_members(client, nsid, a["id"])
        memberships = [(a["id"], pid) for pid in members]
        _keep_changes_since(store, started, [], memberships)
        store.replace_album_members(a["id"], [pid for _, pid in memberships])
    store.upsert_albums(albums)
    store.delete_albums(set(have) - {a["id"] for a in albums})
    out.update(albums_changed=len(changed_albums), albums_deleted=len(set(have) - {a["id"] for a in albums}))

    out["activity"] = sync_activity(client, store, since, log)
    store.set_meta("last_incremental", started)
    return out


def sync_activity(client, store, since: int, log=print) -> int:
    """New comments and faves since `since`: update those photos' counts, re-read the details."""
    hours = min(max(1, math.ceil((time.time() - since) / 3600)), 24 * 30)
    timeframe = f"{hours}h" if hours < 48 else f"{math.ceil(hours / 24)}d"
    touched = set()
    for item in paginate(client, "flickr.activity.userPhotos", "items", "item", timeframe=timeframe):
        pid = str(item["id"])
        if store.photo(pid) is None:
            continue
        store.update_photo(pid, comments=int(item.get("comments") or 0), faves=int(item.get("faves") or 0),
                           views=int(item.get("views") or 0))
        touched.add(pid)
    if touched:
        todo = social.plan(store)
        social.fetch_comments(client, store, [p for p in todo["comments"] if p in touched], log=lambda *a: None)
        social.fetch_faves(client, store, [p for p in todo["faves"] if p in touched], log=lambda *a: None)
    return len(touched)
