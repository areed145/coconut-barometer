"""Where a photo without a geotag was probably taken, from what's known about it:

  nearby   geotagged photos taken around the same time: the same camera first (between the
           shots just before and after, when there are both), then any camera
  title    place names in your title ("Rockfishing, Morro Bay, CA"), checked against the
           state or country named beside them
  tags     your tags that name a place (morrobay, eilat with israel beside it)
  vision   place names in the vision model's title and tags (landmarks it recognized)

Each clue is a point with a score (0-1) and a Flickr accuracy (16 street ... 3 country).
Clues that agree (within CLUSTER_KM, or a state/country containing a town) are merged into
one guess, placed at its strongest clue, whose score grows with each: 1 - (1 - a)(1 - b)...
Towns with the same name are told apart by the state or country written beside them (one in
another state than the one named is ruled out), then by how many of your geotagged photos
were taken near each. Nothing is guessed from words that could be anything: a one-word tag
counts only with its state or country beside it, and tags on a quarter of your photos
(unitedstates, mobile) not at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cached_property

from .gazetteer import Gazetteer, Place, key
from .geo import country_names, haversine_km

CLUSTER_KM = 30
PIN_KM = 10                # a more precise clue this close to the strongest one places the pin
WINDOW_HOURS = 24          # how far either side to look for nearby photos
GRID = 0.5                 # degrees: the cells your geotagged photos are counted in, for telling towns apart

# Weights. Title words are yours and deliberate; tags are often boilerplate; the model's are second-hand.
TITLE, TITLE_QUALIFIED, TITLE_COUNTRY, TITLE_PART = 0.55, 0.8, 0.7, 0.4
TAG, TAG_QUALIFIED = 0.35, 0.5
REGION, COUNTRY = 0.3, 0.25            # a state or country named on its own (title; tags count 2/3 of it)
VISION = 0.75                          # the model's words count this much of the same words of yours
OTHER_CAMERA = 0.7
COMMON_TAG_SHARE = 0.25                # tags on this share of your photos are ignored


@dataclass
class Clue:
    lat: float
    lon: float
    accuracy: int
    score: float
    source: str                 # nearby | title | tags | vision
    reason: str
    cc: str = ""
    admin1: str = ""
    level: str = "point"        # point (a town or a nearby photo) | state | country
    place: str = ""


@dataclass
class Guess:
    lat: float
    lon: float
    accuracy: int
    score: float
    place: str
    sources: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        return "high" if self.score >= 0.75 else "medium" if self.score >= 0.45 else "low"

    def to_dict(self) -> dict:
        return {"lat": round(self.lat, 6), "lon": round(self.lon, 6), "accuracy": self.accuracy,
                "place": self.place, "score": round(self.score, 2), "confidence": self.confidence,
                "sources": self.sources, "reason": "; ".join(self.reasons)}


def parse_taken(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((text or "").strip())
    except ValueError:
        return None


class Guesser:
    """store: for the geotagged photos around a time, and where your photos cluster.
    geocoder: names the nearby photos' places (see geo.Geocoder)."""

    def __init__(self, store, gazetteer: Gazetteer, geocoder):
        self.store, self.gaz, self.geocoder = store, gazetteer, geocoder
        self._density: dict[tuple[int, int], int] | None = None

    def guess(self, photo, vision: dict | None = None, camera: str = "") -> list[Guess]:
        """Every guess for this photo, the likeliest first (none: no clue at all)."""
        clues = self.nearby(photo, camera)
        tags = [t for t in (photo["tags"] or "").split() if t not in self.common]
        mine = self.words(photo["title"] or "", tags, "", 1.0)
        clues += mine
        if vision:                          # read beside your words: your "CA" settles the model's "Carmel"
            context = {key(w) for w in re.split(r"[,;|]", photo["title"] or "")} | {key(t) for t in tags}
            clues += self.words(vision.get("title") or "", [key(t) for t in vision.get("tags") or []],
                                "vision", VISION, context - {""})
        return combine(clues)

    @cached_property
    def common(self) -> set[str]:
        """Tags on so many of your photos (unitedstates, mobile ...) that they say nothing about this one."""
        return self.store.common_tags(COMMON_TAG_SHARE)

    # ---- nearby photos ----------------------------------------------------------------
    def nearby(self, photo, camera: str = "") -> list[Clue]:
        taken = parse_taken(photo["date_taken"])
        if taken is None or photo["date_taken_unknown"]:
            return []
        span = timedelta(hours=WINDOW_HOURS)
        rows = [r for r in self.store.geotagged_between(str(taken - span), str(taken + span)) if r["id"] != photo["id"]]
        same = [r for r in rows if camera and r["camera"] == camera]
        other = [r for r in rows if not (camera and r["camera"] == camera)]
        clues = []
        for group, weight, who in ((same, 1.0, f"same camera ({camera})" if camera else ""),
                                   (other, OTHER_CAMERA, "another camera" if camera else "a photo")):
            c = _between(group, taken, weight, who)
            if c:
                clues.append(c)
        if clues:
            for c, place in zip(clues, self.geocoder([(c.lat, c.lon) for c in clues])):
                c.cc, c.admin1 = place.get("cc", ""), place.get("admin1", "")
                parts = (place.get("name") or "", c.admin1, country_names(c.cc)[0] if c.cc else "")
                c.place = ", ".join(x for x in dict.fromkeys(parts) if x)
        return clues

    # ---- words ------------------------------------------------------------------------
    def words(self, title: str, tags: list[str], source: str, weight: float,
              context: set[str] = frozenset()) -> list[Clue]:
        """Place names in a title and tags; context: other words that may name their state or country.
        source "" means yours: "title" / "tags"."""
        segments = [s.strip() for s in re.split(r"\s*(?:[,;|·•/]|\s[-–—]\s)\s*", title) if key(s)]
        seg_keys = [key(s) for s in segments]
        tag_keys = [key(t) for t in tags if key(t)]
        everything = set(seg_keys) | set(tag_keys) | set(context)
        out: list[Clue] = []
        name = lambda own: source or own

        for seg, k in zip(segments, seg_keys):
            others = everything - {k}
            why = f"“{seg}” in the title"
            town, agree = self._town(k, others)
            regions, country = self.gaz.region(k), self.gaz.country(k)
            if town and (agree > 0 or not (regions or country)):
                # A name with no state or country beside it that several towns share ("Sunset"): a weak clue.
                shared = len(self.gaz.towns(k)) > 1
                score = {2: TITLE_QUALIFIED, 1: TITLE_COUNTRY}.get(agree, TITLE_PART if shared else TITLE)
                out.append(_clue(town, score * weight, name("title"), why))
                continue
            if regions or country:
                out += [_clue(p, REGION * weight, name("title"), why) for p in regions[:3]]
                if country:
                    out.append(_clue(country, COUNTRY * weight, name("title"), why))
                continue
            hit = self._phrase(seg.split(), others)
            if hit:
                town, agree, phrase = hit
                score = {2: TITLE_QUALIFIED, 1: TITLE_COUNTRY}.get(agree, TITLE_PART)
                out.append(_clue(town, score * weight, name("title"), f"“{phrase}” in the title"))

        for k in dict.fromkeys(tag_keys):
            if k in seg_keys or len(k) < 4:
                continue
            others = everything - {k}
            why = f"tag “{k}”"
            town, agree = self._town(k, others)
            # One-word names need their state or country beside them; a multi-word name is telling enough.
            if town and (agree > 0 or (agree == 0 and " " in town.name)):
                out.append(_clue(town, (TAG_QUALIFIED if agree > 0 else TAG) * weight, name("tags"), why))
                continue
            out += [_clue(p, REGION * 2 / 3 * weight, name("tags"), why) for p in self.gaz.region(k)[:3]]
            country = self.gaz.country(k)
            if country:
                out.append(_clue(country, COUNTRY * 2 / 3 * weight, name("tags"), why))
        return out

    def _phrase(self, words: list[str], others: set[str]):
        """A town inside a longer phrase ("Flight to Taft", "Flying to Pismo Beach"): the longest run of
        words that names one. One word counts only with its state or country beside it."""
        for n in range(min(4, len(words) - 1), 0, -1):
            for i in range(len(words) - n + 1):
                phrase = " ".join(words[i:i + n])
                if len(key(phrase)) < 4:
                    continue
                town, agree = self._town(key(phrase), others)
                if town and agree >= (1 if n == 1 else 0):
                    return town, agree, phrase
        return None

    def _town(self, k: str, others: set[str]) -> tuple[Place | None, int]:
        """The town by this name and what the other words say about it (gazetteer.agreement): the one in
        the state or country named beside it, else the one most of your geotagged photos are near.
        None if every one is in another state than the one named."""
        towns = self.gaz.towns(k)
        if not towns:
            return None, 0
        rated = [(self.gaz.agreement(p, others), self.density(p.lat, p.lon), p) for p in towns]
        agree, _, best = max(rated, key=lambda r: r[:2])
        return (best, agree) if agree >= 0 else (None, agree)

    def density(self, lat: float, lon: float) -> int:
        """How many of your geotagged photos are in the cells around this point."""
        if self._density is None:
            self._density = {}
            for la, lo, n in self.store.geotag_cells(GRID):
                self._density[(round(la / GRID), round(lo / GRID))] = n
        i, j = round(lat / GRID), round(lon / GRID)
        return sum(self._density.get((i + a, j + b), 0) for a in (-1, 0, 1) for b in (-1, 0, 1))


def _clue(p: Place, score: float, source: str, reason: str) -> Clue:
    level = "point" if p.level == "city" else p.level
    return Clue(p.lat, p.lon, p.accuracy, score, source, f"{reason} → {p.label}", p.cc, p.admin1, level, p.label)


def _between(rows, taken: datetime, weight: float, who: str) -> Clue | None:
    """From the geotagged photos just before and just after: between the two when both are close in
    time, else the nearer one."""
    timed = [(parse_taken(r["date_taken"]), r) for r in rows]
    before = max(((t, r) for t, r in timed if t and t <= taken), default=None, key=lambda x: x[0])
    after = min(((t, r) for t, r in timed if t and t > taken), default=None, key=lambda x: x[0])
    if not before and not after:
        return None
    hours = lambda t: abs((taken - t).total_seconds()) / 3600
    ago = lambda h: f"{round(h * 60)} min" if h < 1.5 else f"{h:.0f} h"
    if before and after and hours(before[0]) <= 3 and hours(after[0]) <= 3:
        (tb, b), (ta, a) = before, after
        span = (ta - tb).total_seconds()
        f = (taken - tb).total_seconds() / span if span else 0.0
        lat, lon = b["lat"] + (a["lat"] - b["lat"]) * f, b["lon"] + (a["lon"] - b["lon"]) * f
        apart = haversine_km(b["lat"], b["lon"], a["lat"], a["lon"])
        h = min(hours(tb), hours(ta))
        acc = min(b["accuracy"] or 16, a["accuracy"] or 16, 15 if apart < 2 else 13 if apart < 20 else 10)
        score = _time_score(h) * (1.0 if apart < 20 else 0.8) * weight
        return Clue(lat, lon, acc, score, "nearby",
                    f"between {who}'s photos {ago(hours(tb))} before and {ago(hours(ta))} after ({apart:.0f} km apart)")
    t, r = min((x for x in (before, after) if x), key=lambda x: hours(x[0]))
    h = hours(t)
    acc = min(r["accuracy"] or 16, 14 if h <= 2 else 12 if h <= 6 else 11)
    return Clue(r["lat"], r["lon"], acc, _time_score(h) * weight, "nearby",
                f"{who}'s photo {ago(h)} {'before' if t <= taken else 'after'}")


def _time_score(hours: float) -> float:
    return 0.85 if hours <= 0.5 else 0.7 if hours <= 2 else 0.5 if hours <= 6 else 0.3


def _contains(anchor: Clue, c: Clue) -> bool:
    if c.level == "country":
        return bool(c.cc) and anchor.cc == c.cc
    if c.level == "state":
        return bool(c.cc) and anchor.cc == c.cc and anchor.admin1 == c.admin1
    return anchor.level == "point" and haversine_km(anchor.lat, anchor.lon, c.lat, c.lon) <= CLUSTER_KM


def combine(clues: list[Clue]) -> list[Guess]:
    """Clues that agree, merged: each guess sits at its strongest town or photo, the likeliest first."""
    order = sorted(clues, key=lambda c: (-c.accuracy, -c.score))
    groups: list[list[Clue]] = []
    for c in order:
        homes = [g for g in groups if _contains(g[0], c)]
        if not homes:
            groups.append([c])
        for g in homes:                     # a state or country can back several towns in it
            g.append(c)
    out = []
    for g in groups:
        best: dict[tuple[str, str], Clue] = {}      # one say per source and reason: no counting a tag twice
        for c in g:
            k = (c.source, c.reason)
            if k not in best or c.score > best[k].score:
                best[k] = c
        miss, seen = 1.0, set()
        for c in sorted(best.values(), key=lambda c: -c.score):   # each source's strongest clue counts fully,
            miss *= 1 - min(c.score if c.source not in seen else c.score / 2, 0.95)   # the rest half
            seen.add(c.source)
        # The strongest clue says where; a more precise one right there (a photo taken in that town) pins it.
        points = [c for c in g if c.level == "point"]
        anchor = max(points, key=lambda c: (c.score, c.accuracy)) if points else g[0]
        anchor = max((c for c in points if haversine_km(c.lat, c.lon, anchor.lat, anchor.lon) <= PIN_KM),
                     key=lambda c: (c.accuracy, c.score), default=anchor)
        out.append(Guess(anchor.lat, anchor.lon, anchor.accuracy, 1 - miss, anchor.place,
                         list(dict.fromkeys(c.source for c in g)), list(dict.fromkeys(c.reason for c in g))))
    out.sort(key=lambda x: (-x.score, -x.accuracy))
    return out
