"""What every rule gets (Context) and what album/tag rules return (wants).

Album and tag rules only say what they'd like - AlbumWant, TagWant - and one shared
step (resolve) turns those into Changes: the first rule to want something wins, albums
already on Flickr are skipped, and a photo's tags are proposed as one whole list, capped
at Flickr's limit.
Title and description rules return Changes themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from ..changes import Change
from ..classify import fingerprint
from ..config import MAX_TAGS_PER_PHOTO
from ..geo import GeoMatcher, offline_geocoder
from ..geoguess import Guesser
from ..util import clean_tag, norm_tag, norm_title


@dataclass
class AlbumWant:
    photo_id: str
    album: str
    source: str
    confidence: str = ""
    reason: str = ""


@dataclass
class TagWant:
    photo_id: str
    tag: str
    source: str


class Context:
    """The library and settings, shared by every rule. The costlier parts (reverse
    geocoding, vision results, EXIF) load on first use, so rules that are off cost nothing."""

    def __init__(self, cfg: dict, store, provider_key: str | None, geocoder=None,
                 photo_ids: set[str] | None = None, gazetteer=None):
        self.cfg, self.store, self.provider_key, self._geocoder = cfg, store, provider_key, geocoder
        self._gazetteer = gazetteer
        self.photos = {p["id"]: p for p in store.photos() if photo_ids is None or p["id"] in photo_ids}
        self.albums = store.albums()
        self.album_ids = {norm_title(a["title"]): a["id"] for a in self.albums}      # normalized title -> id
        self.album_titles = {norm_title(a["title"]): a["title"] for a in self.albums}
        self.members = store.memberships()
        self.create_missing = cfg["albums"]["create_missing"]

    @cached_property
    def geo(self) -> GeoMatcher:
        return GeoMatcher(self.cfg["geo"], self.album_ids, geocoder=self._geocoder, create_missing=self.create_missing)

    @cached_property
    def places(self) -> dict[str, dict]:
        """photo_id -> nearest place {cc, admin1, name} for geotagged photos."""
        return self.geo.locate(list(self.photos.values()))

    @cached_property
    def guesser(self) -> Guesser:
        """Guesses where photos without a geotag were taken (rules/geotag.py)."""
        from .. import gazetteer
        return Guesser(self.store, self._gazetteer or gazetteer.default(), self._geocoder or offline_geocoder)

    @cached_property
    def fingerprint(self) -> str | None:
        return fingerprint(self.cfg["vision"], self.provider_key) if self.provider_key else None

    def vision(self, photo_id: str) -> dict | None:
        """The cached vision result for the current settings, if there is one."""
        return self.store.classification(photo_id, self.fingerprint) if self.fingerprint else None

    def classified_at(self, photo_id: str) -> int:
        return (self.store.classified_at(photo_id, self.fingerprint) or 0) if self.fingerprint else 0

    @cached_property
    def cameras(self) -> dict[str, str]:
        return self.store.cameras()


def resolve(ctx: Context, wants: list) -> list[Change]:
    """Turn album and tag wants (in priority order) into Changes. Tags: one set_tags per photo
    with every tag wanted, the ones it has already among them, replacing its tags (any it has
    that nothing wants go); none if that's the tags it has. A photo nothing wants tags for keeps its own."""
    albums: dict[str, dict[str, AlbumWant]] = {}   # photo -> normalized album title -> first want
    tags: dict[str, dict[str, TagWant]] = {}       # photo -> normalized tag -> first want
    for w in wants:
        if isinstance(w, AlbumWant):
            albums.setdefault(w.photo_id, {}).setdefault(norm_title(w.album), w)
        elif clean_tag(w.tag):
            tags.setdefault(w.photo_id, {}).setdefault(norm_tag(clean_tag(w.tag)), w)

    out: list[Change] = []
    for pid, by_title in albums.items():
        have = ctx.members.get(pid, set())
        for key, w in by_title.items():
            album_id = ctx.album_ids.get(key, "")
            if not album_id and not ctx.create_missing:
                continue
            title = ctx.album_titles.get(key, w.album)
            if album_id and album_id in have:
                continue
            out.append(Change("add_to_album", pid, title, album_id=album_id, source=w.source, confidence=w.confidence,
                              reason=w.reason if album_id else f"{w.reason} (NEW ALBUM)"))

    cap = MAX_TAGS_PER_PHOTO
    for pid, by_tag in tags.items():
        current = (ctx.photos[pid]["tags"] or "").split()
        keep = list(by_tag.values())[:cap]       # in priority order: over Flickr's limit, the least useful drop off
        if keep and {norm_tag(clean_tag(w.tag)) for w in keep} != {norm_tag(t) for t in current}:
            out.append(Change("set_tags", pid, [clean_tag(w.tag) for w in keep], old=" ".join(current),
                              source=",".join(sorted({w.source for w in keep}))))
    return out
