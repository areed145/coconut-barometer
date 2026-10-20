"""Optional tag groups applied by convention rather than by looking at the photo:
owner names, filler words, date, coordinates, place, camera, and album tags.

Each group has its own `enabled` flag under `standard_tags` in config.yaml. Like
everything else they only add tags, and they go through the reviewed plan.
"""
from __future__ import annotations

import re

from .geo import country_names, haversine_km
from .geo import US_STATES
from .util import norm_tag, norm_title

SEASONS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
           6: "summer", 7: "summer", 8: "summer", 9: "fall", 10: "fall", 11: "fall"}

# Region tags per country, in the style your older uploads use.
_REGION_GROUPS = {
    ("americas", "northamerica"): "US CA MX GL PM BM",
    ("americas", "centralamerica"): "BZ GT SV HN NI CR PA",
    ("caribbean",): "AG KN MS AI VG VI MF SX BL GP MQ DM LC VC GD BB TT AW CW BQ KY TC BS CU JM HT DO PR",
    ("americas", "southamerica"): "AR BO BR CL CO EC GY PY PE SR UY VE GF FK",
    ("europe", "westerneurope"): "PT ES IT FR DE GB IE NL BE LU CH MC AD SM VA MT GI LI NO SE DK FI IS GR FO IM JE GG",
    ("europe", "centraleurope"): "AT HR SK HU CZ PL SI",
    ("europe", "easterneurope"): "UA ME BA RS MK AL RO BG MD BY RU EE LV LT XK",
    ("asia", "middleeast"): "TR JO PS SY IL LB IQ IR SA AE QA KW BH OM YE CY",
    ("asia",): "CN HK MO TW JP KR KP MN IN PK BD LK NP BT MV TH VN LA KH MM MY SG ID PH BN TL "
               "KZ UZ TM KG TJ AF GE AM AZ",
    ("africa", "northafrica"): "MA DZ TN LY EG EH SD",
    ("africa",): "AO NG CD CG CF TZ KE UG RW BI ET ER DJ SO SS ZA NA BW ZW ZM MW MZ MG MU SC KM "
                 "LS SZ GH CI SN ML NE TD CM GA GQ ST BJ TG BF GN GW SL LR GM MR CV RE YT",
    ("oceania",): "AU NZ",
    ("oceania", "pacific"): "PF FJ WS TO VU NC PG SB KI MH FM PW NR TV CK NU WF AS GU MP",
}
REGIONS: dict[str, list[str]] = {cc: list(tags) for tags, ccs in _REGION_GROUPS.items() for cc in ccs.split()}

_DATED = re.compile(r"^y?(19|20)\d\d$")


def _date_parts(date_taken: str | None):
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)", date_taken or "")
    if not m or m.group(1) < "1900":
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return {"year": year, "month": month, "day": day, "season": SEASONS[month]}


def _coord(v: float) -> int:
    """Whole degrees x10, no sign: 30.35 -> 300, -95.42 -> 950."""
    return int(round(abs(v))) * 10


class StandardTagger:
    def __init__(self, scfg: dict, geo_cfg: dict, album_titles: dict[str, str], categories: dict):
        """album_titles: album id -> title."""
        self.cfg = scfg
        self.geo_cfg = geo_cfg
        self.album_titles = album_titles
        acfg = scfg["album_tags"]
        self.album_tags: dict[str, list[str]] = {}
        if acfg["from_categories"]:
            for c in categories.values():
                if c.get("album") and c.get("tags"):
                    self.album_tags.setdefault(norm_title(c["album"]), []).extend(c["tags"])
        for title, tags in (acfg["albums"] or {}).items():
            self.album_tags.setdefault(norm_title(title), []).extend([tags] if isinstance(tags, str) else tags)
        self.camera_rules = [(re.compile(r["match"], re.I), r.get("tags") or []) for r in scfg["camera"]["rules"] or []]
        self.extra_place = {norm_title(k): v for k, v in (scfg["place"]["extra"] or {}).items()}

    def any_enabled(self) -> bool:
        return any(self.cfg[g].get("enabled") for g in
                   ("date", "place", "camera", "album_tags", "coords", "owner", "filler"))

    def needs_places(self) -> bool:
        return bool(self.cfg["place"]["enabled"])

    def tags_for(self, photo, place: dict | None, camera: str | None, album_ids: set[str]) -> list[tuple[str, str]]:
        """[(tag, source)] in priority order, so the most useful survive the per-photo cap."""
        out: list[tuple[str, str]] = []
        have = set((photo["tags"] or "").split())
        add = lambda tags, src: out.extend((t, src) for t in tags if t)

        if self.cfg["date"]["enabled"]:
            add(self.date_tags(photo, have), "std:date")
        if self.cfg["place"]["enabled"] and place:
            add(self.place_tags(photo, place), "std:place")
        if self.cfg["camera"]["enabled"] and camera:
            add(self.camera_tags(camera), "std:camera")
        if self.cfg["album_tags"]["enabled"]:
            for aid in sorted(album_ids):
                add(self.album_tags.get(norm_title(self.album_titles.get(aid, "")), []), "std:album")
        if self.cfg["coords"]["enabled"]:
            add(self.coord_tags(photo), "std:coords")
        if self.cfg["owner"]["enabled"]:
            add(self.cfg["owner"]["tags"] or [], "std:owner")
        if self.cfg["filler"]["enabled"]:
            add(self.cfg["filler"]["tags"] or [], "std:filler")
        return out

    def date_tags(self, photo, have: set[str]) -> list[str]:
        dcfg = self.cfg["date"]
        if photo["date_taken_unknown"]:   # Flickr filled in the upload date; don't tag it
            return []
        if dcfg["skip_if_dated"] and any(_DATED.match(t) for t in have):
            return []
        parts = _date_parts(photo["date_taken"])
        return [f.format(**parts) for f in dcfg["formats"] or []] if parts else []

    def coord_tags(self, photo) -> list[str]:
        ccfg = self.cfg["coords"]
        if photo["lat"] is None or (photo["accuracy"] or 16) < ccfg["min_accuracy"]:
            return []
        return [f.format(lat=_coord(photo["lat"]), lon=_coord(photo["lon"])) for f in ccfg["formats"] or []]

    def place_tags(self, photo, place: dict) -> list[str]:
        pcfg, acc = self.cfg["place"], photo["accuracy"] or 16
        min_acc = self.geo_cfg["min_accuracy"]
        cc, state = place.get("cc", ""), place.get("admin1") or ""
        names: list[str] = []
        if not cc or acc < min_acc["country"]:
            return []
        country = country_names(cc)[0]
        if pcfg["country"]:
            names.append(country)
        if pcfg["regions"]:
            names += (pcfg["region_overrides"] or {}).get(cc) or REGIONS.get(cc, [])
        if state and acc >= min_acc["state"]:
            if pcfg["state"]:
                names.append(state)
            if pcfg["state_abbr"] and cc == "US" and state in US_STATES:
                names.append(US_STATES[state])
        if pcfg["city"] and acc >= min_acc["city"]:
            names.append(self.city_name(photo, place))
        for key in (cc, country, state):
            names += self.extra_place.get(norm_title(key), []) if key else []
        tags = [norm_tag(n) for n in names if n]
        if pcfg["split_words"]:
            tags += [norm_tag(w) for n in names if n and " " in n.strip() for w in n.split()]
        return tags

    def city_name(self, photo, place: dict) -> str:
        # A configured city (by distance) wins over the nearest town, as with renames.
        for c in self.geo_cfg["cities"]:
            if haversine_km(photo["lat"], photo["lon"], c["lat"], c["lon"]) <= c.get("radius_km", 25):
                return c.get("tag") or c.get("name") or c["album"]
        return place.get("name") or ""

    def camera_tags(self, camera: str) -> list[str]:
        tags = [norm_tag(w) for w in camera.split()] if self.cfg["camera"]["name_words"] else []
        for rx, extra in self.camera_rules:
            if rx.search(camera):
                tags += extra
        return tags

    def camera_albums(self, camera: str) -> list[str]:
        return [r["album"] for r in self.cfg["camera"]["albums"] or [] if re.search(r["match"], camera, re.I)]
