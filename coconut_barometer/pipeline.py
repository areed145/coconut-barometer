"""Per-photo pipeline that fills the review queue: (refresh) -> EXIF -> classify -> plan.

Photos go through in small batches, so a run can be stopped and resumed: a photo is
queued only once all of its steps succeeded, and queued photos are skipped next time.
"""
from __future__ import annotations

import time

from .camera import fetch_cameras
from .classify import classify_photos, fingerprint, pending_photos
from .geo import locations_for
from .planner import build_plan
from .sync import refresh_photo


def process(cfg: dict, store, client, provider, photo_ids: list[str] | None = None, limit: int | None = None,
            rebuild: bool = False, refresh_each: bool = False, log=print, geocoder=None, downloader=None,
            media: str | None = None, tag_mode: str = "add", reclassify: bool = False) -> dict:
    """media: only "photo" or only "video" (videos are classified from their preview frame).
    tag_mode: "add" to existing tags, or "replace" them (you still approve each photo).
    reclassify: ask the vision model again even when a result for the current settings is cached.
    Without a vision provider, only the proposals that don't need it are made."""
    vcfg = cfg["vision"]
    use_vision = provider is not None
    fp = fingerprint(vcfg, provider.key) if use_vision else None
    # Never touch a photo the review tool is applying right now; otherwise skip queued ones unless rebuilding.
    # ("new" photos are queued only to be processed: always fair game.)
    skip = set(store.queue_ids(["applying"])) | (set() if rebuild else set(store.queue_ids()) - set(store.queue_ids(["new"])))
    todo = [p["id"] for p in store.photos() if p["id"] not in skip and (not photo_ids or p["id"] in photo_ids)
            and (not media or p["media"] == media)]
    todo = todo[:limit] if limit else todo
    stats = {"queued": 0, "nothing_to_change": 0, "retry_later": 0, "input_tokens": 0, "output_tokens": 0}
    batch = max(8, int(vcfg["concurrency"]) * 4)
    quiet = lambda *a: None
    classify_kw = {"downloader": downloader} if downloader else {}

    for start in range(0, len(todo), batch):
        ids = todo[start:start + batch]
        if refresh_each:
            for pid in ids:
                refresh_photo(client, store, pid, albums=True, thumb_key=vcfg["thumbnail"])
        # EXIF (cached forever once fetched)
        have_exif = store.camera_ids()
        fetch_cameras(client, store, [pid for pid in ids if pid not in have_exif], log=quiet)
        ok = set(ids) & store.camera_ids()
        # Classify the ones without a result for the current settings
        if use_vision:
            if reclassify:
                need = [p for p in map(store.photo, ids) if p and p["thumb_url"]]
                started = int(time.time())
            else:
                need = pending_photos(store, vcfg, fp, photo_ids=ids)
            if need:
                locations = locations_for(need, cfg["geo"]["min_accuracy"], geocoder)
                s = classify_photos(store, provider, vcfg, need, log=quiet, locations=locations, **classify_kw)
                stats["input_tokens"] += s["input_tokens"]
                stats["output_tokens"] += s["output_tokens"]
            ok -= {p["id"] for p in pending_photos(store, vcfg, fp, photo_ids=ids)}   # still missing = failed
            if reclassify:                      # an old result left in place isn't a new look
                ok -= {p["id"] for p in need if (store.classified_at(p["id"], fp) or 0) < started}
        # Plan and queue
        rows = build_plan(cfg, store, provider.key if use_vision else None, geocoder=geocoder, photo_ids=ok,
                          tag_mode=tag_mode)
        changes: dict[str, list[dict]] = {}
        for r in rows:
            changes.setdefault(r.photo_id, []).append(r.to_dict())
        for pid in ids:
            if pid not in ok:
                stats["retry_later"] += 1
                continue
            store.queue_put(pid, "pending" if pid in changes else "none", changes.get(pid, []))
            stats["queued" if pid in changes else "nothing_to_change"] += 1
        log(f"  {min(start + batch, len(todo))}/{len(todo)} photos · {stats['queued']} to review")
    return stats
