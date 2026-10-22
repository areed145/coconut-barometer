"""Location albums from the geotag: country, US state, and your cities (by distance).
(Location tags are place tags: see conventions.)"""
from __future__ import annotations

from .base import AlbumWant, Context


def propose(ctx: Context) -> list:
    return [AlbumWant(s.photo_id, s.album_title, f"geo:{s.level}", reason=s.reason)
            for s in ctx.geo.suggest(list(ctx.photos.values()), ctx.places)]
