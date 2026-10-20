"""Optional: retitle photos that have a generic name (IMG_1234, DSC01234, blank…)
with their location, e.g. "Houston, TX" or "Luanda, Angola"."""
from __future__ import annotations

import re

from .geo import country_names, haversine_km
from .util import norm_title as norm_tag_words

US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN",
    "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "Washington, D.C.": "DC", "District of Columbia": "DC",
}

# Camera / phone / app default file names, plus empty-ish titles.
DEFAULT_GENERIC_PATTERNS = [
    r"",                                         # blank
    r"(IMG|_MG|MVI|VID|DSC|DSCN|DSCF|SAM|GOPR|GH\d\d|GX\d\d|DJI|PANO|PXL|MOV|CIMG|IMAG|P\d{3})[_\-]?[A-Z]?\d[\w\-]*",
    r"\d{8}[_\-]\d{6}(\w*)",                    # 20240101_123456
    r"(IMG|VID|PXL)?[_\-]?\d{8}[_\-]?(WA)?\d*",  # IMG-20240101-WA0001
    r"\d+",                                      # 1234
    r"(photo|image|picture|pic|untitled|snapshot|screenshot)[\s_\-]*\d*",
]


class Renamer:
    def __init__(self, rcfg: dict, geo_cfg: dict):
        self.cfg = rcfg
        self.cities = geo_cfg["cities"]
        pats = rcfg["generic_patterns"] or DEFAULT_GENERIC_PATTERNS
        pats = list(pats) + list(rcfg["extra_generic_patterns"] or [])
        self.generic = [re.compile(rf"^\s*(?:{p})(\.(jpe?g|heic|png|dng|raw|cr2|nef|arw|tiff?|mov|mp4))?\s*$", re.I)
                        for p in pats]

    def is_generic(self, title: str) -> bool:
        return any(rx.match(title or "") for rx in self.generic)

    def is_place_only(self, photo, place: dict | None) -> bool:
        """A title that just names where it was taken, maybe numbered: "Seattle, WA",
        "Luanda, Angola", "Bellagio 175". Anything with words of your own is not."""
        words = re.findall(r"[a-z]+", norm_tag_words(photo["title"]))
        if not words:
            return bool(re.search(r"\d", photo["title"] or ""))
        region = {"usa", "us", "uk"}   # state and country words from the geotag
        known = set(region)
        if place:
            state = place.get("admin1") or ""
            for name in [state, US_STATES.get(state), *country_names(place.get("cc") or "")]:
                region.update(re.findall(r"[a-z]+", norm_tag_words(name)))
            known |= region | set(re.findall(r"[a-z]+", norm_tag_words(place.get("name"))))
        for c in self.cities:
            known.update(re.findall(r"[a-z]+", norm_tag_words(c.get("name") or c["album"])))
        if set(words) <= known:
            return True
        # "Seattle, WA" / "Kyoto, Japan" when the geotag's nearest town is a neighbour:
        # a short first part, then only the geotag's state or country.
        parts = [x for x in (photo["title"] or "").split(",") if x.strip()]
        rest = re.findall(r"[a-z]+", norm_tag_words(",".join(parts[1:])))
        if len(parts) >= 2 and rest and set(rest) <= region and len(parts[0].split()) <= 3:
            return True
        # Not geotagged (or the geocoder's town differs): "Bellagio 175", "Panama City 799"
        # (checked on the accent-stripped form: "Foz do Iguaçu 625" may store ç as c + a combining mark)
        return bool(re.fullmatch(r"[a-z][a-z ]{0,30}?\s+\d+", norm_tag_words(photo["title"]))) and len(words) <= 3

    def replaceable(self, photo, place: dict | None, mode: str) -> bool:
        """Which existing titles a proposed title may replace: generic | generic_or_place | all."""
        if mode == "all":
            return True
        return self.is_generic(photo["title"]) or (mode == "generic_or_place" and self.is_place_only(photo, place))

    def city_name(self, photo, place: dict) -> str | None:
        # A configured city (by distance) wins over the geocoder's nearest town, so a
        # photo in a suburb still reads "Austin, TX" if you've defined Austin.
        for c in self.cities:
            if haversine_km(photo["lat"], photo["lon"], c["lat"], c["lon"]) <= c.get("radius_km", 25):
                return c.get("name") or c["album"]
        return place.get("name") or None

    def title_for(self, photo, place: dict) -> str | None:
        cc = place.get("cc", "")
        if not cc:
            return None
        acc = photo["accuracy"] or 16
        state = place.get("admin1") or ""
        fields = {
            "state": state,
            "state_abbr": US_STATES.get(state, state),
            "region": state,
            "country": country_names(cc)[0],
            "cc": cc,
        }
        if acc >= self.cfg["min_accuracy"]:
            city = self.city_name(photo, place)
            if not city:
                return None
            fmt = self.cfg["us_format"] if cc == "US" else self.cfg["intl_format"]
            return fmt.format(city=city, **fields).strip(", ")
        if self.cfg["fallback_to_region"] and acc >= 6:
            fmt = self.cfg["us_region_format"] if cc == "US" else self.cfg["intl_region_format"]
            return fmt.format(**fields).strip(", ")
        return None

    def suggest(self, photos, places: dict[str, dict]) -> list[tuple[str, str, str]]:
        """Return [(photo_id, old_title, new_title)]."""
        out = []
        for p in photos:
            place = places.get(p["id"])
            if not place or not self.is_generic(p["title"]):
                continue
            new = self.title_for(p, place)
            if new and new != p["title"]:
                out.append((p["id"], p["title"], new))
        return out
