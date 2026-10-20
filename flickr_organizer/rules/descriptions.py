"""Descriptions from the vision model: blank ones get one proposal per photo; with
describe.reword, one this tool wrote and you haven't edited is re-worded once per
brief change (see changes.Settled)."""
from __future__ import annotations

from ..changes import Change
from .base import Context


def propose(ctx: Context) -> list[Change]:
    dcfg = ctx.cfg["vision"]["describe"]
    if not dcfg["enabled"] or not ctx.fingerprint:
        return []
    reword = dcfg["reword"]
    out = []
    for pid, p in ctx.photos.items():
        text = ((ctx.vision(pid) or {}).get("description") or "").strip()
        if not text:
            continue
        current = (p["description"] or "").strip()
        if not current:
            if ctx.settled.blank_description(pid):
                continue
            reason = ""
        elif reword and ctx.settled.may_reword(pid, current, text, ctx.classified_at(pid)):
            reason = "re-worded with the current brief (the old text was written by this tool)"
        else:
            continue
        out.append(Change("set_description", pid, text, old=current, source="vision:describe", reason=reason))
    return out
