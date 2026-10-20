"""Descriptions: the model's description for every photo, unless it says the same as the
one there or you've dealt with that proposal. A new brief makes new text, so a new proposal."""
from __future__ import annotations

from ..changes import Change
from ..util import same_text
from .base import Context


def propose(ctx: Context) -> list[Change]:
    out = []
    for pid, p in ctx.photos.items():
        text = ((ctx.vision(pid) or {}).get("description") or "").strip()
        current = (p["description"] or "").strip()
        if not text or same_text(text, current) or ctx.settled.description(pid, text):
            continue
        out.append(Change("set_description", pid, text, old=current, source="vision:describe"))
    return out
