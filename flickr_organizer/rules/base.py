"""What every rule gets (Context) and what album/tag rules return (wants).

Album and tag rules only say what they'd like - AlbumWant, TagWant - and one shared
step (resolve) turns those into Changes: the first rule to want something wins, things
already on Flickr or settled are skipped, and tags are capped at Flickr's limit.
Title and description rules return Changes themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from ..changes import Change, Settled
from ..classify import fingerprint
from ..geo import GeoMatcher
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
                 photo_ids: set[str] | None = None):
        self.cfg, self.store, self.provider_key, self._geocoder = cfg, store, provider_key, geocoder
        self.photos = {p["id"]: p for p in store.photos() if photo_ids is None or p["id"] in photo_ids}
        self.albums = store.albums()
        self.album_ids = {norm_title(a["title"]): a["id"] for a in self.albums}      # normalized title -> id
        self.album_titles = {norm_title(a["title"]): a["title"] for a in self.albums}
        self.members = store.memberships()
        self.settled = Settled(store)
        self.create_missing = cfg["albums"].get("create_missing", False)

    @cached_property
    def geo(self) -> GeoMatcher:
        return GeoMatcher(self.cfg["geo"], self.album_ids, geocoder=self._geocoder, create_missing=self.create_missing)

    @cached_property
    def places(self) -> dict[str, dict]:
        """photo_id -> nearest place {cc, admin1, name} for geotagged photos."""
        return self.geo.locate(list(self.photos.values()))

    @cached_property
    def fingerprint(self) -> str | None:
        vcfg = self.cfg["vision"]
        if vcfg.get("enabled", True) and self.provider_key and vcfg["categories"]:
            return fingerprint(vcfg, self.provider_key)
        return None

    def vision(self, photo_id: str) -> dict | None:
        """The cached vision result for the current settings, if there is one."""
        return self.store.classification(photo_id, self.fingerprint) if self.fingerprint else None

    def classified_at(self, photo_id: str) -> int:
        return (self.store.classified_at(photo_id, self.fingerprint) or 0) if self.fingerprint else 0

    @cached_property
    def cameras(self) -> dict[str, str]:
        return self.store.cameras()


def resolve(ctx: Context, wants: list) -> list[Change]:
    """Turn album and tag wants (in priority order) into Changes."""
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
            if (album_id and album_id in have) or ctx.settled.album(pid, title):
                continue
            out.append(Change("add_to_album", pid, title, album_id=album_id, source=w.source, confidence=w.confidence,
                              reason=w.reason if album_id else f"{w.reason} (NEW ALBUM)"))

    cap = int(ctx.cfg["standard_tags"].get("max_tags_per_photo", 75))
    for pid, by_tag in tags.items():
        have = {norm_tag(t) for t in (ctx.photos[pid]["tags"] or "").split()}
        new = [w for key, w in by_tag.items() if key not in have and not ctx.settled.tag(pid, clean_tag(w.tag))]
        # Flickr caps tags per photo; wants are in priority order, so the least useful drop off.
        new = new[:max(0, cap - len(have))]
        if new:
            out.append(Change("add_tags", pid, [clean_tag(w.tag) for w in new],
                              source=",".join(sorted({w.source for w in new}))))
    return out
