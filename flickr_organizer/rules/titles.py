"""Titles: the model's proposal where vision.title.replace allows it, otherwise the
location rename ("City, ST") for camera-default titles. Settled per proposed title."""
from __future__ import annotations

from ..changes import Change
from ..rename import Renamer
from .base import Context


def propose(ctx: Context) -> list[Change]:
    rcfg, tcfg = ctx.cfg.get("rename", {}), ctx.cfg["vision"]["title"]
    if not rcfg.get("enabled") and not (tcfg.get("enabled") and ctx.fingerprint):
        return []
    renamer = Renamer(rcfg, ctx.cfg["geo"])
    chosen: dict[str, tuple[str, str]] = {}   # photo_id -> (title, source)
    if rcfg.get("enabled"):
        for pid, _old, new in renamer.suggest(list(ctx.photos.values()), ctx.places):
            if not ctx.settled.title(pid, new):
                chosen[pid] = (new, "geo:rename")
    if tcfg.get("enabled") and ctx.fingerprint:
        mode = tcfg.get("replace", "generic_or_place")
        for pid, p in ctx.photos.items():
            title = ((ctx.vision(pid) or {}).get("title") or "").strip(" \"'.\n")
            if not title or title == p["title"] or ctx.settled.title(pid, title):
                continue
            if renamer.replaceable(p, ctx.places.get(pid), mode):
                chosen[pid] = (title, "vision:title")      # the model's title wins
    return [Change("set_title", pid, title, old=ctx.photos[pid]["title"] or "", source=source,
                   reason=f"was: {ctx.photos[pid]['title'] or '(blank)'}")
            for pid, (title, source) in chosen.items()]
