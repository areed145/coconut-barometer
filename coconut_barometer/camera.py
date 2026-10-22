"""Fetch each photo's camera, lens and full EXIF once and cache it locally.

Flickr's photo list doesn't include the camera, so this costs one getExif call per
photo. It's incremental: only photos without a cached camera are fetched.
"""
from __future__ import annotations

from .flickr_client import FlickrCallError



def parse_exif(entries: list[dict]) -> dict[str, str]:
    """Flickr's EXIF list -> {tag: value}. Prefers Flickr's readable `clean` form
    ("1/250 sec", "f/5.6") over the raw one; the first tagspace wins on duplicates."""
    out: dict[str, str] = {}
    for e in entries:
        value = (e.get("clean") or e.get("raw") or {}).get("_content", "")
        if e.get("tag") and e["tag"] not in out and value != "":
            out[e["tag"]] = value
    return out


def pending_cameras(store, limit: int | None = None) -> list[str]:
    done = store.camera_ids()
    out = [p["id"] for p in store.photos() if p["id"] not in done]
    return out[:limit] if limit else out


def fetch_cameras(client, store, photo_ids: list[str], log=print) -> dict:
    stats = {"ok": 0, "no_camera": 0, "failed": 0}
    for i, pid in enumerate(photo_ids, 1):
        try:
            r = client.call("flickr.photos.getExif", photo_id=pid)["photo"]
        except FlickrCallError as e:
            if e.code is not None:  # Flickr said no (not found, no EXIF, e.g. some videos): cache "none"
                store.save_camera(pid, "", "", "")
                stats["no_camera"] += 1
            else:  # network trouble etc.: not cached, so retried next run
                stats["failed"] += 1
                log(f"  ! {pid}: {e}")
            continue
        exif = parse_exif(r.get("exif", []))
        camera = r.get("camera") or ""
        lens = exif.get("LensModel") or exif.get("Lens") or ""
        store.save_camera(pid, camera, exif.get("Make", ""), exif.get("Model", ""), lens, exif)
        stats["ok" if camera else "no_camera"] += 1
        if i % 100 == 0 or i == len(photo_ids):
            log(f"  {i}/{len(photo_ids)} photos")
    return stats
