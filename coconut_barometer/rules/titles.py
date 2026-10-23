"""Titles: the model's title for every photo, unless it's the title already."""
from __future__ import annotations

from ..changes import Change
from .base import Context


def propose(ctx: Context) -> list[Change]:
    out = []
    for pid, p in ctx.photos.items():
        title = ((ctx.vision(pid) or {}).get("title") or "").strip(" \"'.\n")
        if not title or title == (p["title"] or ""):
            continue
        out.append(Change("set_title", pid, title, old=p["title"] or "", source="vision:title",
                          reason=f"was: {p['title'] or '(blank)'}"))
    return out
