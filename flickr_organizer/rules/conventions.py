"""Tags you apply by convention (standard_tags: date, place, camera, album, coords,
owner, filler) and, from the camera, your camera albums."""
from __future__ import annotations

from ..standard_tags import StandardTagger
from .base import AlbumWant, Context, TagWant


def propose(ctx: Context) -> list:
    scfg = ctx.cfg["standard_tags"]
    std = StandardTagger(scfg, ctx.cfg["geo"], {a["id"]: a["title"] for a in ctx.albums},
                         ctx.cfg["vision"]["categories"])
    if not std.any_enabled() and not scfg["camera"]["albums"]:
        return []
    places = ctx.places if std.needs_places() else {}
    wants: list = []
    for pid, p in ctx.photos.items():
        camera = ctx.cameras.get(pid) or ""      # camera tags only if switched on (tags_for); albums always
        wants += [TagWant(pid, t, source) for t, source in std.tags_for(p, places.get(pid), camera,
                                                                         ctx.members.get(pid, set()))]
        if camera:
            wants += [AlbumWant(pid, title, "std:camera", reason=f"camera: {camera}")
                      for title in std.camera_albums(camera)]
    return wants
