"""Engagement data: your groups and which photos are in each pool, comments, and faves.

View/fave/comment *counts* come free with `sync`; this fetches the details behind them,
only where a photo's count changed since the last fetch, so later runs are cheap.
Comments and faves are saved per photo, so a run can be stopped and resumed.
"""
from __future__ import annotations

import json
import time

from .flickr_client import FlickrCallError, paginate


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def plan(store) -> dict[str, list[str]]:
    """Photo ids whose comments / faves need (re)fetching: the count changed since last time."""
    out = {}
    for kind, col in (("comments", "comments"), ("faves", "faves")):
        seen = store.social_counts(kind)
        out[kind] = [p["id"] for p in store.photos()
                     if p[col] is not None and (p[col] or 0) != seen.get(p["id"], 0)]
    return out


def fetch_groups(client, store, log=print) -> dict:
    nsid = store.get_meta("nsid") or client.nsid()
    groups, pool = [], []
    for g in paginate(client, "flickr.groups.pools.getGroups", "groups", "group"):
        gid = str(g.get("nsid") or g["id"])
        name = g.get("name") or g.get("title") or gid
        if isinstance(name, dict):
            name = name.get("_content", gid)
        groups.append({"id": gid, "name": name, "path_alias": g.get("path_alias") or g.get("pathalias"),
                       "members": _int(g.get("members") or g.get("member_count"), None),
                       "pool_count": _int(g.get("pool_count") or g.get("photos"), None),
                       "privacy": str(g.get("privacy", "")), "info": json.dumps(g), "fetched_at": int(time.time())})
    log(f"  {len(groups)} groups")
    failed = 0
    for i, g in enumerate(groups, 1):
        try:
            for p in paginate(client, "flickr.groups.pools.getPhotos", "photos", "photo",
                              group_id=g["id"], user_id=nsid):
                pool.append((g["id"], str(p["id"]), _int(p.get("dateadded"), None)))
        except FlickrCallError as e:   # e.g. a group you can no longer see
            failed += 1
            log(f"  ! {g['name']}: {e}")
        if i % 25 == 0:
            log(f"  {i}/{len(groups)} group pools")
    store.replace_groups(groups, pool)
    return {"groups": len(groups), "photos_in_pools": len(pool), "failed_groups": failed}


def fetch_comments(client, store, photo_ids: list[str], log=print) -> dict:
    counts = {p["id"]: p["comments"] or 0 for p in store.photos()}
    stats = {"photos": 0, "comments": 0, "failed": 0}
    for i, pid in enumerate(photo_ids, 1):
        if not counts.get(pid):
            store.save_comments(pid, [], 0)            # all deleted: nothing to fetch
            continue
        try:
            r = client.call("flickr.photos.comments.getList", photo_id=pid).get("comments", {})
        except FlickrCallError as e:
            stats["failed"] += 1
            log(f"  ! {pid} comments: {e}")
            continue
        rows = [{"id": str(c["id"]), "photo_id": pid, "author": c.get("author"), "author_name": c.get("authorname"),
                 "real_name": c.get("realname"), "date_created": _int(c.get("datecreate"), None),
                 "permalink": c.get("permalink"), "text": c.get("_content", "")} for c in r.get("comment", [])]
        store.save_comments(pid, rows, counts[pid])
        stats["photos"] += 1
        stats["comments"] += len(rows)
        if i % 100 == 0:
            log(f"  {i}/{len(photo_ids)} photos' comments")
    return stats


def fetch_faves(client, store, photo_ids: list[str], log=print) -> dict:
    counts = {p["id"]: p["faves"] or 0 for p in store.photos()}
    stats = {"photos": 0, "faves": 0, "failed": 0}
    for i, pid in enumerate(photo_ids, 1):
        if not counts.get(pid):
            store.save_faves(pid, [], 0)
            continue
        rows, page = [], 1
        try:
            while True:   # 50 per page is getFavorites' maximum
                r = client.call("flickr.photos.getFavorites", photo_id=pid, per_page=50, page=page)["photo"]
                rows += [{"photo_id": pid, "user_id": f.get("nsid"), "username": f.get("username"),
                          "real_name": f.get("realname"), "date_faved": _int(f.get("favedate"), None)}
                         for f in r.get("person", [])]
                if page >= _int(r.get("pages"), 1):
                    break
                page += 1
        except FlickrCallError as e:
            stats["failed"] += 1
            log(f"  ! {pid} faves: {e}")
            continue
        store.save_faves(pid, rows, counts[pid])
        stats["photos"] += 1
        stats["faves"] += len(rows)
        if i % 100 == 0:
            log(f"  {i}/{len(photo_ids)} photos' faves")
    return stats
