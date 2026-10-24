"""Watches, warnings and advisories for the Weather map (#87): the NWS's alerts in effect now, each with its area.

The NWS API (api.weather.gov/alerts/active) only gives a shape for storm-based warnings; watches, advisories and most
others name forecast zones instead. NOAA's own GIS mirror of the same alerts (the `WWA/watch_warn_adv` MapServer,
layer 1) has the zones' shapes attached, one feature per alert per zone, generalized server-side, so that's what's
fetched: every TTL while the map asks, only if it changed (ETag), in the background (it takes a few seconds). The
features are merged per alert (`cap_id`, the NWS API's id), anything past its expiration is dropped even if the
service hasn't caught up, and each gets the NWS hazard map's color and priority (HAZARDS, from weather.gov/help-map:
the most important first, so it's drawn on top). Shapes are kept at two levels of detail, the way radar's are
(shapes.py): fine for the view zoomed in, coarse for the US zoomed out.

A popup's headline, area and instruction come from the NWS API's /alerts/{id} when it's opened (detail()), kept a
few minutes. Advisories, statements and marine alerts are `minor`: the map shows them only when asked (a whole
coastline of Small Craft Advisories otherwise). One per process (`hazards`)."""
from __future__ import annotations

import json
import threading
from datetime import datetime

from ..gazetteer import USER_AGENT
from . import shapes
from .feed import Feed

URL = "https://mapservices.weather.noaa.gov/eventdriven/rest/services/WWA/watch_warn_adv/MapServer/1/query"
DETAIL_URL = "https://api.weather.gov/alerts/{}"
TTL, DETAIL_TTL = 120, 300
PAGE = 2000              # features asked for at a time (the service's limit is 4000)
GENERALIZE = 0.01        # degrees the service may move a shape's points (maxAllowableOffset)
LEVELS = {"fine": 100, "coarse": 20}     # name -> coordinates in whole 1/q degrees (0.01°, 0.05°)
FINE_ZOOM = 6            # radar.py's: from here in, the fine shapes for the view
DETAILS_KEPT = 200

# The NWS hazard map's events, most important first (their priority is their place here, from 1), and colors.
HAZARDS = {name: (i + 1, color) for i, (name, color) in enumerate([
    ("Tsunami Warning", "#FD6347"), ("Tornado Warning", "#FF0000"), ("Extreme Wind Warning", "#FF8C00"),
    ("Severe Thunderstorm Warning", "#FFA500"), ("Flash Flood Warning", "#8B0000"),
    ("Flash Flood Statement", "#8B0000"), ("Severe Weather Statement", "#00FFFF"),
    ("Shelter In Place Warning", "#FA8072"), ("Evacuation Immediate", "#7FFF00"), ("Civil Danger Warning", "#FFB6C1"),
    ("Nuclear Power Plant Warning", "#4B0082"), ("Radiological Hazard Warning", "#4B0082"),
    ("Hazardous Materials Warning", "#4B0082"), ("Fire Warning", "#A0522D"), ("Civil Emergency Message", "#FFB6C1"),
    ("Law Enforcement Warning", "#C0C0C0"), ("Storm Surge Warning", "#B524F7"),
    ("Hurricane Force Wind Warning", "#CD5C5C"), ("Hurricane Warning", "#DC143C"), ("Typhoon Warning", "#DC143C"),
    ("Special Marine Warning", "#FFA500"), ("Blizzard Warning", "#FF4500"), ("Snow Squall Warning", "#C71585"),
    ("Ice Storm Warning", "#8B008B"), ("Heavy Freezing Spray Warning", "#00BFFF"), ("Winter Storm Warning", "#FF69B4"),
    ("Lake Effect Snow Warning", "#008B8B"), ("Dust Storm Warning", "#FFE4C4"), ("Blowing Dust Warning", "#FFE4C4"),
    ("High Wind Warning", "#DAA520"), ("Tropical Storm Warning", "#B22222"), ("Storm Warning", "#9400D3"),
    ("Tsunami Advisory", "#D2691E"), ("Tsunami Watch", "#FF00FF"), ("Avalanche Warning", "#1E90FF"),
    ("Earthquake Warning", "#8B4513"), ("Volcano Warning", "#2F4F4F"), ("Ashfall Warning", "#A9A9A9"),
    ("Flood Warning", "#00FF00"), ("Coastal Flood Warning", "#228B22"), ("Lakeshore Flood Warning", "#228B22"),
    ("Ashfall Advisory", "#696969"), ("High Surf Warning", "#228B22"), ("Extreme Heat Warning", "#C71585"),
    ("Tornado Watch", "#FFFF00"), ("Severe Thunderstorm Watch", "#DB7093"), ("Flash Flood Watch", "#2E8B57"),
    ("Gale Warning", "#DDA0DD"), ("Flood Statement", "#00FF00"), ("Extreme Cold Warning", "#0000FF"),
    ("Freeze Warning", "#483D8B"), ("Red Flag Warning", "#FF1493"), ("Storm Surge Watch", "#DB7FF7"),
    ("Hurricane Watch", "#FF00FF"), ("Hurricane Force Wind Watch", "#9932CC"), ("Typhoon Watch", "#FF00FF"),
    ("Tropical Storm Watch", "#F08080"), ("Storm Watch", "#FFE4B5"), ("Tropical Cyclone Local Statement", "#FFE4B5"),
    ("Winter Weather Advisory", "#7B68EE"), ("Avalanche Advisory", "#CD853F"), ("Cold Weather Advisory", "#AFEEEE"),
    ("Heat Advisory", "#FF7F50"), ("Flood Advisory", "#00FF7F"), ("Coastal Flood Advisory", "#7CFC00"),
    ("Lakeshore Flood Advisory", "#7CFC00"), ("High Surf Advisory", "#BA55D3"), ("Dense Fog Advisory", "#708090"),
    ("Dense Smoke Advisory", "#F0E68C"), ("Small Craft Advisory", "#D8BFD8"), ("Brisk Wind Advisory", "#D8BFD8"),
    ("Hazardous Seas Warning", "#D8BFD8"), ("Dust Advisory", "#BDB76B"), ("Blowing Dust Advisory", "#BDB76B"),
    ("Lake Wind Advisory", "#D2B48C"), ("Wind Advisory", "#D2B48C"), ("Frost Advisory", "#6495ED"),
    ("Freezing Fog Advisory", "#008080"), ("Freezing Spray Advisory", "#00BFFF"), ("Low Water Advisory", "#A52A2A"),
    ("Local Area Emergency", "#C0C0C0"), ("Winter Storm Watch", "#4682B4"), ("Rip Current Statement", "#40E0D0"),
    ("Beach Hazards Statement", "#40E0D0"), ("Gale Watch", "#FFC0CB"), ("Avalanche Watch", "#F4A460"),
    ("Hazardous Seas Watch", "#483D8B"), ("Heavy Freezing Spray Watch", "#BC8F8F"), ("Flood Watch", "#2E8B57"),
    ("Coastal Flood Watch", "#66CDAA"), ("Lakeshore Flood Watch", "#66CDAA"), ("High Wind Watch", "#B8860B"),
    ("Extreme Heat Watch", "#800000"), ("Extreme Cold Watch", "#5F9EA0"), ("Freeze Watch", "#00FFFF"),
    ("Fire Weather Watch", "#FFDEAD"), ("Extreme Fire Danger", "#E9967A"), ("911 Telephone Outage", "#C0C0C0"),
    ("Coastal Flood Statement", "#6B8E23"), ("Lakeshore Flood Statement", "#6B8E23"),
    ("Special Weather Statement", "#FFE4B5"), ("Marine Weather Statement", "#FFDAB9"),
    ("Air Quality Alert", "#808080"), ("Air Stagnation Advisory", "#808080"), ("Hazardous Weather Outlook", "#EEE8AA"),
    ("Hydrologic Outlook", "#90EE90"), ("Short Term Forecast", "#98FB98"), ("Administrative Message", "#C0C0C0"),
    ("Test", "#F0FFFF"), ("Child Abduction Emergency", "#FFFFFF"), ("Blue Alert", "#FFFFFF"),
])}
# Anything not in HAZARDS: a color by significance (warning, watch, advisory, statement), after all of them.
BY_SIG = {"W": "#FF4500", "A": "#FFD700", "Y": "#87CEEB", "S": "#FFE4B5"}
# Alerts for the water, shown with the advisories: they'd paint the whole coast.
MARINE = {"Special Marine Warning", "Hurricane Force Wind Warning", "Gale Warning", "Storm Warning", "Hazardous Seas Warning",
          "Heavy Freezing Spray Warning", "Gale Watch", "Storm Watch", "Hurricane Force Wind Watch", "Hazardous Seas Watch",
          "Heavy Freezing Spray Watch", "Small Craft Advisory", "Brisk Wind Advisory", "Freezing Spray Advisory",
          "Low Water Advisory", "Marine Weather Statement"}
TOP = 16                 # HAZARDS' first 16 (tornado warnings, flash flood statements, civil emergencies) are never minor


def look(event: str, sig: str) -> tuple[int, str, bool]:
    """An event's (priority, color, minor)."""
    if event in HAZARDS:
        priority, color = HAZARDS[event]
    else:
        priority, color = len(HAZARDS) + 1 + "WAYS".find(sig or "S"), BY_SIG.get(sig, BY_SIG["S"])
    major = priority <= TOP or (event.endswith(("Warning", "Watch")) and event not in MARINE) \
        or (event not in HAZARDS and sig in ("W", "A"))
    return priority, color, not major


def _time(s: str | None) -> int | None:
    try:
        return int(datetime.fromisoformat(s).timestamp()) if s and s.strip() else None
    except ValueError:
        return None


def _polygons(geometry: dict | None) -> list[list]:
    """A GeoJSON Polygon or MultiPolygon -> its polygons, each [outer ring, hole, ...] of [lon, lat] points."""
    if not geometry:
        return []
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return list(geometry["coordinates"])
    return []


def _encode(polygons: list[list], q: int) -> list[list]:
    out = []
    for polygon in polygons:
        rings = [shapes.ring(r, q) for r in polygon]
        if rings and rings[0] is not None:
            out.append(shapes.poly([rings[0], *(r for r in rings[1:] if r is not None)]))
    return out


def alerts_from(features: list[dict], now: float) -> list[dict]:
    """The service's features -> one alert per cap_id, unexpired, most important first:
    {id, event, sig, wfo, onset, ends, expires, priority, color, minor, polygons, levels: {fine, coarse}}."""
    by_id: dict[str, dict] = {}
    for f in features:
        p = f.get("properties") or {}
        event = (p.get("prod_type") or "").strip()
        expires = _time(p.get("expiration"))
        if not event or (expires is not None and expires <= now):
            continue
        key = (p.get("cap_id") or "").strip() or f"{event}|{p.get('wfo')}|{p.get('expiration')}"
        a = by_id.get(key)
        if a is None:
            sig = (p.get("sig") or "").strip()
            priority, color, minor = look(event, sig)
            a = by_id[key] = {"id": key, "event": event, "sig": sig, "wfo": (p.get("wfo") or "").strip(),
                              "onset": _time(p.get("onset")), "ends": _time(p.get("ends")), "expires": expires,
                              "priority": priority, "color": color, "minor": minor, "polygons": []}
        a["polygons"] += _polygons(f.get("geometry"))
    out = []
    for a in by_id.values():
        fine = _encode(a["polygons"], LEVELS["fine"])
        if not fine:
            continue
        # A small area can round away entirely at the coarse level: then it keeps its fine shapes there too.
        a["levels"] = {"fine": fine, "coarse": _encode(a["polygons"], LEVELS["coarse"]) or fine}
        out.append(a)
    return sorted(out, key=lambda a: (a["priority"], a["id"]))


class Hazards(Feed):
    source, what, via, ttl = "imagery", "watches & warnings", "NOAA WWA MapServer", TTL

    def __init__(self, *a, **k):
        k.setdefault("background", True)
        super().__init__(*a, **k)
        self._details: dict[str, tuple[float, dict]] = {}
        self._details_lock = threading.Lock()

    def load(self, a: dict) -> dict | None:
        features, offset = [], 0
        while True:
            params = {"where": "1=1", "outFields": "prod_type,sig,phenom,onset,ends,expiration,wfo,cap_id", "outSR": 4326,
                      "maxAllowableOffset": GENERALIZE, "geometryPrecision": 3, "orderByFields": "objectid",
                      "resultOffset": offset, "resultRecordCount": PAGE, "f": "geojson"}
            r = self._get(URL, params=params, headers={"User-Agent": USER_AGENT, **(self.conditional() if not offset else {})}, timeout=60)
            if r.status_code == 304:
                a["detail"] = "no change"
                return None
            r.raise_for_status()
            if not offset:
                first = r
            page = r.json().get("features") or []
            features += page
            if len(page) < PAGE or offset > 10 * PAGE:
                break
            offset += PAGE
        alerts = alerts_from(features, self._clock())
        a["detail"] = f"{len(alerts)} alerts ({sum(not x['minor'] for x in alerts)} watches and warnings), {len(features)} areas"
        self.remember(first)
        return {"alerts": alerts}

    def view(self, bbox: tuple | None = None, zoom: float | None = None, now: float | None = None) -> tuple[str, dict]:
        """The alerts in effect at `now` touching `bbox`, as JSON text (the shapes spliced in, as radar's are: the
        fine level from FINE_ZOOM in, else the coarse one), and the feed's state (checked_at, error, pending)."""
        state = self.current()
        level = "fine" if zoom is not None and zoom >= FINE_ZOOM else "coarse"
        q, now = LEVELS[level], now or self._clock()
        out = []
        for a in (state["data"] or {}).get("alerts", []):
            if a["expires"] is not None and a["expires"] <= now:
                continue
            texts = [p[4] for p in a["levels"][level] if shapes.in_view(p, bbox, q)]
            if texts:
                meta = {k: a[k] for k in ("id", "event", "sig", "wfo", "onset", "ends", "expires", "priority", "color", "minor")}
                out.append(json.dumps(meta, separators=(",", ":"))[:-1] + ',"polys":[' + ",".join(texts) + "]}")
        return f'{{"level":"{level}","q":{q},"alerts":[{",".join(out)}]}}', state

    def known(self, alert_id: str) -> bool:
        return any(a["id"] == alert_id for a in ((self.data or {}).get("alerts") or []))

    def detail(self, alert_id: str) -> dict:
        """The NWS API's text for an alert on the map: {headline, area, sender, description, instruction}, kept a
        few minutes."""
        now = self._clock()
        with self._details_lock:
            kept = self._details.get(alert_id)
            if kept and now - kept[0] < DETAIL_TTL:
                return kept[1]
        from .. import activity

        with activity.log.call(self.source, "alert text", "NWS API") as a:
            r = self._get(DETAIL_URL.format(alert_id), headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"}, timeout=20)
            r.raise_for_status()
            p = r.json().get("properties") or {}
            a["detail"] = p.get("event") or ""
        out = {"headline": p.get("headline"), "area": p.get("areaDesc"), "sender": p.get("senderName"),
               "description": p.get("description"), "instruction": p.get("instruction")}
        with self._details_lock:
            self._details[alert_id] = (now, out)
            for k in sorted(self._details, key=lambda k: self._details[k][0])[:-DETAILS_KEPT]:
                del self._details[k]
        return out


hazards = Hazards()
