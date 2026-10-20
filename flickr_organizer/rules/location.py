"""Location albums from the geotag: country, US state, and your cities (by distance).
With geo.add_tags, also tags such as "japan", "texas", "austin"."""
from __future__ import annotations

from .base import AlbumWant, Context, TagWant


def propose(ctx: Context) -> list:
    gcfg = ctx.cfg["geo"]
    if not gcfg["enabled"]:
        return []
    wants: list = []
    for s in ctx.geo.suggest(list(ctx.photos.values()), ctx.places):
        wants.append(AlbumWant(s.photo_id, s.album_title, f"geo:{s.level}", reason=s.reason))
        if gcfg["add_tags"] and s.tag:
            wants.append(TagWant(s.photo_id, s.tag, f"geo:{s.level}"))
    return wants
