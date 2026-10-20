"""Pull your photo list and album membership from Flickr into the local store."""
from __future__ import annotations

import time

from .flickr_client import PHOTO_EXTRAS, paginate
from .store import Store
from .util import norm_tag


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _keep_changes_since(store: Store, since: int, photos: list[dict], memberships: list[tuple[str, str]]) -> int:
    by_id = {p["id"]: p for p in photos}
    n = 0
    for pid, action, target in store.applied_since(since):
        p = by_id.get(pid)
        if p is None:
            continue
        n += 1
        if action == "set_title":
            p["title"] = target
        elif action == "set_description":
            p["description"] = target
        elif action == "add_tag":
            if norm_tag(target) not in (p["tags"] or "").split():
                p["tags"] = f"{p['tags']} {norm_tag(target)}".strip()
        elif action == "add_to_album":
            album_id = store.album_id(target)
            if album_id:
                memberships.append((album_id, pid))
    return n


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def normalize_photo(p: dict, thumb_key: str) -> dict:
    lat, lon = _f(p.get("latitude")), _f(p.get("longitude"))
    if not lat and not lon:  # Flickr returns 0/0 for "no location"
        lat = lon = None
    desc = p.get("description")
    if isinstance(desc, dict):
        desc = desc.get("_content", "")
    thumb = p.get(thumb_key) or p.get("url_m") or p.get("url_z") or p.get("url_n") or p.get("url_q")
    return {
        "id": str(p["id"]),
        "title": p.get("title") or "",
        "description": desc or "",
        "tags": p.get("tags") or "",
        "lat": lat,
        "lon": lon,
        "accuracy": int(p.get("accuracy") or 0) if lat is not None else None,
        "media": p.get("media", "photo"),
        "thumb_url": thumb,
        "secret": p.get("secret"),
        "server": p.get("server"),
        "date_taken": p.get("datetaken"),
        "date_upload": int(p.get("dateupload") or 0),
        "last_update": int(p.get("lastupdate") or 0),
        "ispublic": int(p.get("ispublic") or 0),
        "date_taken_unknown": int(p.get("datetakenunknown") or 0),
        "views": _int(p.get("views")),
        "faves": _int(p.get("count_faves")),
        "comments": _int(p.get("count_comments")),
        "license": p.get("license"),
        "width": _int(p.get("o_width")),
        "height": _int(p.get("o_height")),
    }


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


def sync(client, store: Store, thumb_key: str = "url_m", log=print) -> dict:
    started = int(time.time())
    nsid = client.nsid()
    store.set_meta("nsid", nsid)

    log("Fetching photo list…")
    photos = []
    for p in paginate(client, "flickr.people.getPhotos", "photos", "photo", user_id="me", extras=PHOTO_EXTRAS):
        photos.append(normalize_photo(p, thumb_key))
        if len(photos) % 2000 == 0:
            log(f"  {len(photos)} photos")
    log(f"  {len(photos)} photos total")

    log("Fetching albums…")
    albums = []
    for a in paginate(client, "flickr.photosets.getList", "photosets", "photoset", user_id=nsid):
        title = a.get("title")
        if isinstance(title, dict):
            title = title.get("_content", "")
        albums.append({"id": str(a["id"]), "title": title or "", "count": int(a.get("photos", 0)) + int(a.get("videos", 0))})
    log(f"  {len(albums)} albums")

    log("Fetching album membership…")
    memberships = []
    for i, a in enumerate(albums, 1):
        for p in paginate(client, "flickr.photosets.getPhotos", "photoset", "photo",
                          photoset_id=a["id"], user_id=nsid, media="all"):
            memberships.append((a["id"], str(p["id"])))
        if i % 25 == 0:
            log(f"  {i}/{len(albums)} albums")

    # A sync takes minutes; anything applied meanwhile (e.g. from the review tool) may be
    # missing from pages fetched earlier, so lay those changes back over what was fetched.
    kept = _keep_changes_since(store, started, photos, memberships)
    if kept:
        log(f"  kept {kept} change(s) applied during the sync")
    store.replace_library(photos, albums, memberships)
    try:  # account-wide totals; needs stats enabled (Flickr Pro), so skip quietly if not
        store.save_account_stats(client.call("flickr.stats.getTotalViews").get("stats", {}))
    except Exception:
        pass
    stats = {"photos": len(photos), "albums": len(albums), "memberships": len(memberships)}
    log(f"Synced: {stats}")
    return stats
