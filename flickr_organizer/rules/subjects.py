"""Subject albums and tags from the vision model: each category it scores at or above
vision.min_confidence, plus its extra descriptive tags (landmarks, aircraft, species...)."""
from __future__ import annotations

from .base import AlbumWant, Context, TagWant


def propose(ctx: Context) -> list:
    if not ctx.fingerprint:
        return []
    vcfg = ctx.cfg["vision"]
    min_conf = float(vcfg["min_confidence"])
    extra = vcfg["extra_tags"]
    prefix = extra["prefix"] or ""
    wants: list = []
    for pid in ctx.photos:
        res = ctx.vision(pid)
        if not res:
            continue
        scores = res.get("scores", {})
        for cat, c in vcfg["categories"].items():
            conf = float(scores.get(cat, 0) or 0)
            if conf < min_conf:
                continue
            if c.get("album"):
                wants.append(AlbumWant(pid, c["album"], f"vision:{cat}", f"{conf:.2f}", res.get("caption", "")))
            wants += [TagWant(pid, t, f"vision:{cat}") for t in c.get("tags", []) or []]
        if extra["enabled"]:
            wants += [TagWant(pid, prefix + t, "vision:tags") for t in (res.get("tags") or [])[: extra["max"]]]
    return wants
