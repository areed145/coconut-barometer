"""A geotag for photos without one (review.missing_location): the likeliest guess from nearby
photos, the title and tags, and the vision model's words (see geoguess.py), with the others to
choose from. No clue at all still proposes one, empty: you pick the place on the map."""
from __future__ import annotations

from ..changes import Change
from .base import Context


def propose(ctx: Context) -> list[Change]:
    if not ctx.cfg["review"]["missing_location"]:
        return []
    out = []
    for pid, p in ctx.photos.items():
        if p["lat"] is not None:
            continue
        guesses = ctx.guesser.guess(p, ctx.vision(pid), ctx.cameras.get(pid, ""))
        if not guesses:
            out.append(Change("set_location", pid, {}, source="geo:guess",
                              reason="no clue where: search the map, or click where it was taken"))
            continue
        best = guesses[0]
        out.append(Change("set_location", pid, {"lat": round(best.lat, 6), "lon": round(best.lon, 6),
                                                "accuracy": best.accuracy, "place": best.place},
                          source="geo:" + ",".join(best.sources), confidence=best.confidence,
                          reason="; ".join(best.reasons), options=[g.to_dict() for g in guesses[:6]]))
    return out
