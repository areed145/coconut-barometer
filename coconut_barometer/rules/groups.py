"""Group pools to add a photo to (groups.suggest): the ones it matches best among those you're in,
judged by the photos you've already put in each (see grouping.py). Only public photos: groups
show only those."""
from __future__ import annotations

from ..changes import Change
from .base import Context


def propose(ctx: Context) -> list[Change]:
    gcfg = ctx.cfg["groups"]
    if not gcfg["suggest"] or not gcfg["max_per_photo"]:
        return []
    out = []
    refused = ctx.store.refused_groups()       # by a group's rules, or the photo's in all the groups it may be
    for pid, p in ctx.photos.items():
        no = refused.get(pid, set())
        if not p["ispublic"] or "*" in no:
            continue
        extra = (ctx.vision(pid) or {}).get("tags") or []
        matches = [m for m in ctx.groups.match(pid, extra, gcfg["max_per_photo"] + len(no), gcfg["min_match"])
                   if m.name not in no]
        for m in matches[:gcfg["max_per_photo"]]:
            out.append(Change("add_to_group", pid, m.name, album_id=m.group_id, source="groups:match",
                              confidence=m.confidence,
                              reason=f"shares {', '.join(m.shared[:5])} with your {m.pool} photos in it"))
    return out
