"""Place names -> coordinates, offline and online.

Offline: the GeoNames towns (population 1000+, ~145k) that ship with reverse_geocoder, plus
their states/regions and countries (each placed at the mean of its towns). It reads the words
on a photo when guessing a missing geotag (see geoguess.py), and backs up the reviewer's
place search.

Online: OpenStreetMap's Nominatim, for the reviewer's place search (landmarks, parks and
addresses too). It's asked only when you search, at most once a second, as its usage policy asks.
"""
from __future__ import annotations

import csv
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from . import activity
from .geo import US_STATES, country_names

ACCURACY = {"city": 11, "state": 6, "country": 3}   # Flickr's scale: 1 world ... 16 street
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "coconut-barometer/0.1 (personal Flickr organizer)"


def key(text: str) -> str:
    """How names are matched: lowercase, no accents, spaces or punctuation (as Flickr writes tags)."""
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.casefold())


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float
    cc: str
    admin1: str = ""
    level: str = "city"         # city | state | country

    @property
    def accuracy(self) -> int:
        return ACCURACY[self.level]

    @property
    def label(self) -> str:
        """"Salinas, California, United States"; "Colorado, United States"; "Angola"."""
        parts = [self.name] if self.level != "country" else []
        if self.admin1 and self.admin1 != self.name and self.level == "city":
            parts.append(self.admin1)
        parts.append(country_names(self.cc)[0])
        return ", ".join(parts)


class Gazetteer:
    """rows: dicts with lat, lon, name, admin1, cc (reverse_geocoder's CSV columns)."""

    def __init__(self, rows):
        self.cities: dict[str, list[Place]] = {}
        sums: dict[tuple[str, str], list[float]] = {}
        for r in rows:
            p = Place(r["name"], float(r["lat"]), float(r["lon"]), r["cc"], r.get("admin1") or "")
            self.cities.setdefault(key(p.name), []).append(p)
            for k in ((p.cc, p.admin1), (p.cc, "")):
                s = sums.setdefault(k, [0.0, 0.0, 0])
                s[0] += p.lat
                s[1] += p.lon
                s[2] += 1
        self.regions: dict[str, list[Place]] = {}       # state / region name (and US postal code) -> places
        self.countries: dict[str, Place] = {}           # any of a country's names -> the country
        for (cc, admin1), (lat, lon, n) in sums.items():
            if admin1:
                p = Place(admin1, lat / n, lon / n, cc, admin1, "state")
                names = [admin1] + ([US_STATES[admin1]] if cc == "US" and admin1 in US_STATES else [])
                for name in names:
                    self.regions.setdefault(key(name), []).append(p)
            else:
                names = country_names(cc)
                p = Place(names[0], lat / n, lon / n, cc, "", "country")
                for name in names:
                    if len(key(name)) > 2:              # never a bare code like "ca"
                        self.countries.setdefault(key(name), p)

    def towns(self, k: str) -> list[Place]:
        return self.cities.get(k, [])

    def region(self, k: str) -> list[Place]:
        return self.regions.get(k, [])

    def country(self, k: str) -> Place | None:
        return self.countries.get(k)

    def agreement(self, place: Place, words: set[str]) -> int:
        """What these keys say about a town: 2 they name its state (or US postal code), 1 its country,
        -1 another state of its country (and not its own), 0 nothing."""
        own = {key(place.admin1), US_STATES.get(place.admin1, "").lower() if place.cc == "US" else ""} - {""}
        if own & words:
            return 2
        if any(r.cc == place.cc and r.admin1 != place.admin1 for w in words for r in self.region(w)):
            return -1
        return 1 if any(key(n) in words for n in country_names(place.cc)) else 0

    def search(self, query: str, limit: int = 8) -> list[Place]:
        """"Salinas, CA" or "Morro Bay": towns first (those matching the words after the comma
        first), then states and countries by that name."""
        parts = [key(p) for p in query.split(",") if key(p)]
        if not parts:
            return []
        quals = set(parts[1:])
        towns = sorted(self.towns(parts[0]), key=lambda p: -self.agreement(p, quals))
        if quals:
            towns = [p for p in towns if self.agreement(p, quals) > 0] or towns
        out = towns + self.region(parts[0])
        if self.country(parts[0]):
            out.append(self.country(parts[0]))
        return out[:limit]


_lock = threading.Lock()          # loading the towns once
_online_lock = threading.Lock()   # spacing Nominatim requests


@lru_cache(maxsize=1)
def _load() -> Gazetteer:
    import reverse_geocoder

    path = os.path.join(os.path.dirname(reverse_geocoder.__file__), "rg_cities1000.csv")
    with open(path, encoding="utf-8") as f:
        return Gazetteer(csv.DictReader(f))


def default() -> Gazetteer:
    """The GeoNames gazetteer, read once per process (~1 s)."""
    with _lock:
        return _load()


_last_online = [0.0]


def search_online(query: str, limit: int = 8, get=None) -> list[dict]:
    """Nominatim's matches: [{label, lat, lon, accuracy, bbox: [south, north, west, east]}]."""
    if get is None:
        import requests
        get = requests.get
    with _online_lock:                              # its policy: at most one request a second
        wait = _last_online[0] + 1.0 - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_online[0] = time.time()
    with activity.log.call("geocode", "place search", query[:80]) as a:
        r = get(NOMINATIM, params={"q": query, "format": "jsonv2", "limit": limit},
                headers={"User-Agent": USER_AGENT}, timeout=10)
        r.raise_for_status()
        rows = r.json()
        a["detail"] = f"{query[:80]} · {len(rows)} found"
    out = []
    for row in rows:
        bbox = [float(x) for x in row.get("boundingbox") or []]
        out.append({"label": row.get("display_name", ""), "lat": float(row["lat"]), "lon": float(row["lon"]),
                    "accuracy": _accuracy(row.get("place_rank")), "bbox": bbox if len(bbox) == 4 else None})
    return out


def _accuracy(rank) -> int:
    """Nominatim's place_rank (4 country ... 30 building) on Flickr's 1-16 scale."""
    try:
        rank = int(rank)
    except (TypeError, ValueError):
        return 11
    return 3 if rank <= 4 else 6 if rank <= 8 else 11 if rank <= 16 else 13 if rank <= 22 else 16


def search(query: str, limit: int = 8, gazetteer: Gazetteer | None = None, get=None) -> dict:
    """The reviewer's place search: Nominatim, or the offline towns when it can't be reached."""
    try:
        return {"source": "nominatim", "places": search_online(query, limit, get)}
    except Exception as e:     # offline, refused, rate-limited
        places = (gazetteer or default()).search(query, limit)
        return {"source": "offline", "error": str(e)[:200],
                "places": [{"label": p.label, "lat": p.lat, "lon": p.lon, "accuracy": p.accuracy, "bbox": None}
                           for p in places]}
