"""Turn photo coordinates into country / US state / city album suggestions.

Reverse geocoding runs offline with the `reverse_geocoder` package (GeoNames data,
no API calls, ~1s for thousands of points). It returns the nearest populated place,
so photos taken right on a border can occasionally land on the wrong side.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

from .util import norm_title

# Friendlier names for countries whose ISO/pycountry names are awkward.
COMMON_NAMES: dict[str, list[str]] = {
    "US": ["United States", "USA", "United States of America"],
    "GB": ["United Kingdom", "UK", "Great Britain"],
    "KR": ["South Korea", "Korea"],
    "KP": ["North Korea"],
    "RU": ["Russia"],
    "VN": ["Vietnam", "Viet Nam"],
    "TW": ["Taiwan"],
    "IR": ["Iran"],
    "SY": ["Syria"],
    "LA": ["Laos"],
    "BO": ["Bolivia"],
    "VE": ["Venezuela"],
    "TZ": ["Tanzania"],
    "MD": ["Moldova"],
    "CZ": ["Czech Republic", "Czechia"],
    "TR": ["Turkey", "Türkiye"],
    "NL": ["Netherlands", "The Netherlands", "Holland"],
    "AE": ["United Arab Emirates", "UAE"],
    "CD": ["DR Congo", "Democratic Republic of the Congo"],
    "CG": ["Congo", "Republic of the Congo"],
    "CI": ["Ivory Coast", "Côte d'Ivoire"],
    "MK": ["North Macedonia", "Macedonia"],
    "PS": ["Palestine"],
    "VA": ["Vatican City", "Vatican"],
    "BN": ["Brunei"],
    "FM": ["Micronesia"],
    "HK": ["Hong Kong"],
    "MO": ["Macau", "Macao"],
    "PR": ["Puerto Rico"],
}


_WARNED = False


def country_names(cc: str) -> list[str]:
    names = list(COMMON_NAMES.get(cc, []))
    try:
        import pycountry

        c = pycountry.countries.get(alpha_2=cc)
        if c:
            for attr in ("common_name", "name", "official_name"):
                v = getattr(c, attr, None)
                if v and v not in names:
                    names.append(v)
    except ImportError:
        global _WARNED
        if not _WARNED:
            print("warning: pycountry not installed; country auto-matching only knows a few names "
                  "(pip install pycountry, or add geo.countries.aliases)")
            _WARNED = True
    return names or [cc]


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# A geocoder takes [(lat, lon), ...] and returns [{"cc", "admin1", "name"}, ...].
Geocoder = Callable[[Sequence[tuple[float, float]]], list[dict]]


def offline_geocoder(points):
    import reverse_geocoder as rg

    if not points:
        return []
    return rg.search(list(points), mode=1, verbose=False)


@dataclass
class GeoSuggestion:
    photo_id: str
    album_title: str
    level: str          # country | state | city
    reason: str
    tag: str | None = None


class GeoMatcher:
    def __init__(self, cfg: dict, album_titles: dict[str, str], geocoder: Geocoder | None = None,
                 create_missing: bool = False):
        """album_titles: normalized title -> album id (existing albums)."""
        self.cfg = cfg
        self.album_titles = album_titles
        self.geocoder = geocoder or offline_geocoder
        self.min_acc = cfg["min_accuracy"]
        self.create_missing = create_missing

    def country_album(self, cc: str) -> str | None:
        aliases = self.cfg["countries"].get("aliases", {}).get(cc)
        candidates = ([aliases] if isinstance(aliases, str) else list(aliases or []))
        if self.cfg["countries"].get("auto_match", True):
            candidates += country_names(cc)
        for c in candidates:
            if norm_title(c) in self.album_titles:
                return c
        # No existing album: return the preferred name so create_missing can use it.
        return candidates[0] if candidates and self.create_missing else None

    def state_album(self, state: str) -> str | None:
        title = self.cfg["us_states"].get("title", "{state}").format(state=state)
        if norm_title(title) in self.album_titles or self.create_missing:
            return title
        return None

    def locate(self, photos: list) -> dict[str, dict]:
        """Reverse-geocode every geotagged photo once: photo_id -> {cc, admin1, name}."""
        located = [p for p in photos if p["lat"] is not None and p["lon"] is not None]
        if not located:
            return {}
        places = self.geocoder([(p["lat"], p["lon"]) for p in located])
        return {p["id"]: dict(place) for p, place in zip(located, places)}

    def suggest(self, photos: list, places: dict[str, dict] | None = None) -> list[GeoSuggestion]:
        if not self.cfg.get("enabled", True):
            return []
        if places is None:
            places = self.locate(photos)
        out: list[GeoSuggestion] = []
        for p in photos:
            place = places.get(p["id"])
            if not place:
                continue
            acc = p["accuracy"] or 16  # missing accuracy: assume precise GPS
            cc = place.get("cc", "")
            where = f"{place.get('name')}, {place.get('admin1')}, {cc}"
            if cc and acc >= self.min_acc["country"]:
                title = self.country_album(cc)
                if title:
                    out.append(GeoSuggestion(p["id"], title, "country", f"geotag → {where}", country_names(cc)[0]))
            if cc == "US" and self.cfg["us_states"].get("enabled", True) and acc >= self.min_acc["state"]:
                state = place.get("admin1")
                title = self.state_album(state) if state else None
                if title:
                    out.append(GeoSuggestion(p["id"], title, "state", f"geotag → {where}", state))
            if acc >= self.min_acc["city"]:
                for city in self.cfg.get("cities", []):
                    d = haversine_km(p["lat"], p["lon"], city["lat"], city["lon"])
                    if d <= city.get("radius_km", 25):
                        if norm_title(city["album"]) in self.album_titles or self.create_missing:
                            out.append(GeoSuggestion(p["id"], city["album"], "city",
                                                     f"{d:.1f} km from {city['album']} center", city.get("tag", city["album"])))
        return out


def place_label(photo, place: dict | None, min_acc: dict) -> str:
    """Human-readable location for a geotagged photo, only as precise as its geotag:
    "Seattle, Washington, United States", "Washington, United States" or "United States"."""
    if not place or not place.get("cc"):
        return ""
    acc = photo["accuracy"] or 16
    parts = []
    if acc >= min_acc["city"] and place.get("name"):
        parts.append(place["name"])
    if acc >= min_acc["state"] and place.get("admin1") and place.get("admin1") != place.get("name"):
        parts.append(place["admin1"])
    if acc >= min_acc["country"]:
        parts.append(country_names(place["cc"])[0])
    return ", ".join(parts)


def locations_for(photos: list, min_acc: dict, geocoder: Geocoder | None = None) -> dict[str, str]:
    """photo_id -> place_label for the geotagged ones (offline reverse geocoding)."""
    located = [p for p in photos if p["lat"] is not None and p["lon"] is not None]
    if not located:
        return {}
    places = (geocoder or offline_geocoder)([(p["lat"], p["lon"]) for p in located])
    return {p["id"]: place_label(p, dict(place), min_acc) for p, place in zip(located, places)}
