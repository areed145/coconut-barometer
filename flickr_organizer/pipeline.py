"""Per-photo pipeline that fills the review queue: (refresh) -> EXIF -> classify -> plan.

Photos go through in small batches, so a run can be stopped and resumed: a photo is
queued only once all of its steps succeeded, and queued photos are skipped next time.
"""
from __future__ import annotations

from dataclasses import asdict

from .camera import fetch_cameras
from .classify import classify_photos, fingerprint, pending_photos
from .geo import locations_for
from .planner import build_plan


def parse_info(info: dict) -> dict:
    """flickr.photos.getInfo -> the photos-table fields it can refresh."""
    dates = info.get("dates") or {}
    out = {
        "title": (info.get("title") or {}).get("_content", ""),
        "description": (info.get("description") or {}).get("_content", ""),
        "tags": " ".join(t["_content"] for t in (info.get("tags") or {}).get("tag", [])),
        "date_taken": dates.get("taken"),
        "date_taken_unknown": int(dates.get("takenunknown") or 0),
        "last_update": int(dates.get("lastupdate") or 0),
    }
    loc = info.get("location")
    if loc and loc.get("latitude") not in (None, "", 0, "0"):
        out.update(lat=float(loc["latitude"]), lon=float(loc["longitude"]), accuracy=int(loc.get("accuracy") or 16))
    return out


def refresh_photo(client, store, photo_id: str, albums: bool = False) -> None:
    """Re-read one photo from Flickr (1 call; +1 with albums) into the local store."""
    store.update_photo(photo_id, **parse_info(client.call("flickr.photos.getInfo", photo_id=photo_id)["photo"]))
    if albums:
        sets = client.call("flickr.photos.getAllContexts", photo_id=photo_id).get("set", [])
        store.set_photo_albums(photo_id, [str(s["id"]) for s in sets])


def process(cfg: dict, store, client, provider, photo_ids: list[str] | None = None, limit: int | None = None,
            rebuild: bool = False, refresh_each: bool = False, log=print, geocoder=None, downloader=None,
            include_videos: bool | None = None, media: str | None = None) -> dict:
    """include_videos: classify videos (from their preview frame) regardless of vision.include_videos.
    media: only "photo" or only "video"."""
    vcfg = cfg["vision"] if include_videos is None else {**cfg["vision"], "include_videos": include_videos}
    use_vision = provider is not None and vcfg.get("enabled", True) and bool(vcfg["categories"])
    fp = fingerprint(vcfg, provider.key) if use_vision else None
    # Never touch a photo the review tool is applying right now; otherwise skip queued ones unless rebuilding.
    skip = set(store.queue_ids(["applying"])) | (set() if rebuild else set(store.queue_ids()))
    todo = [p["id"] for p in store.photos() if p["id"] not in skip and (not photo_ids or p["id"] in photo_ids)
            and (not media or p["media"] == media)]
    todo = todo[:limit] if limit else todo
    stats = {"queued": 0, "nothing_to_change": 0, "retry_later": 0, "input_tokens": 0, "output_tokens": 0}
    batch = max(8, int(vcfg.get("concurrency", 4)) * 4)
    quiet = lambda *a: None
    classify_kw = {"downloader": downloader} if downloader else {}

    for start in range(0, len(todo), batch):
        ids = todo[start:start + batch]
        if refresh_each:
            for pid in ids:
                refresh_photo(client, store, pid, albums=True)
        # EXIF (cached forever once fetched)
        have_exif = store.camera_ids()
        fetch_cameras(client, store, [pid for pid in ids if pid not in have_exif], log=quiet)
        ok = set(ids) & store.camera_ids()
        # Classify the ones without a result for the current settings
        if use_vision:
            need = pending_photos(store, vcfg, fp, photo_ids=ids)
            if need:
                locations = locations_for(need, cfg["geo"]["min_accuracy"], geocoder)
                s = classify_photos(store, provider, vcfg, need, log=quiet, locations=locations, **classify_kw)
                stats["input_tokens"] += s["input_tokens"]
                stats["output_tokens"] += s["output_tokens"]
            ok -= {p["id"] for p in pending_photos(store, vcfg, fp, photo_ids=ids)}   # still missing = failed
        # Plan and queue
        rows = build_plan(cfg, store, provider.key if use_vision else None, geocoder=geocoder, photo_ids=ok)
        changes: dict[str, list[dict]] = {}
        for r in rows:
            changes.setdefault(r.photo_id, []).append(asdict(r))
        for pid in ids:
            if pid not in ok:
                stats["retry_later"] += 1
                continue
            store.queue_put(pid, "pending" if pid in changes else "none", changes.get(pid, []))
            stats["queued" if pid in changes else "nothing_to_change"] += 1
        log(f"  {min(start + batch, len(todo))}/{len(todo)} photos · {stats['queued']} to review")
    return stats
