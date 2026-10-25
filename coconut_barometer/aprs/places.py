"""Places (#121): named circles in Settings → APRS → Places (Home, Work, Cabin), and whether a tracked callsign is
in one, for the arrive and leave alerts and for naming a trip's stops (#120).

A callsign is in a place when a position falls within its radius, and out when one falls beyond LEAVE × the
radius; between the two nothing changes, so GPS jitter at the edge doesn't flap. A change holds once two positions
in a row agree, or one has stood for the dwell time with nothing since saying otherwise (beacons are sparse when
parked). The first position seen sets where it is without an alert."""
from __future__ import annotations

import re

from ..geo import haversine_km

LEAVE = 1.2
RADIUS_M = 200
QUIET = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")
EVENTS = ("arrive", "leave", "both")


def places(cfg: dict) -> list[dict]:
    """Settings → APRS → Places, each with its radius (m) filled in."""
    return [{"name": str(p["name"]).strip(), "lat": float(p["lat"]), "lon": float(p["lon"]),
             "radius_m": float(p.get("radius_m") or RADIUS_M)} for p in cfg["aprs"].get("places") or []]


def distance_m(place: dict, lat: float, lon: float) -> float:
    return haversine_km(place["lat"], place["lon"], lat, lon) * 1000


def named(cfg: dict, lat: float, lon: float) -> str | None:
    """The nearest place a point is within, if any (for a trip's stops)."""
    hits = [(d, p["name"]) for p in places(cfg) if (d := distance_m(p, lat, lon)) <= p["radius_m"]]
    return min(hits)[1] if hits else None


def quiet_minutes(text: str) -> tuple[int, int] | None:
    """"22:00-07:00" as minutes of the day (start, end); None when empty. ValueError when unreadable."""
    if not (text or "").strip():
        return None
    m = QUIET.match(text)
    if not m:
        raise ValueError(text)
    h1, m1, h2, m2 = map(int, m.groups())
    return h1 * 60 + m1, h2 * 60 + m2


def is_quiet(text: str, minute: int) -> bool:
    span = quiet_minutes(text)
    if not span:
        return False
    start, end = span
    return start <= minute < end if start <= end else minute >= start or minute < end


def step(state: dict | None, inside: bool | None, ts: int, at=None) -> tuple[dict, str | None]:
    """One position's verdict (True: within the radius, False: beyond LEAVE × it, None: in between) applied to
    {"in": bool, "pending": None | {"in": bool, "first": ts, "count": n, "at": the first position}}. Returns the new
    state and "arrive" or "leave" when a change holds."""
    if state is None:                          # the first position: where it is, no alert
        return {"in": bool(inside), "pending": None, "since": ts}, None
    state = {**state}
    if inside is None or inside == state["in"]:
        state["pending"] = None                # back where it was (or at the edge): any change in the making is off
        return state, None
    p = state.get("pending")
    p = {"in": inside, "first": ts, "count": 1, "at": at} if not p or p["in"] != inside else {**p, "count": p["count"] + 1}
    state["pending"] = p
    if p["count"] >= 2:
        return settle(state)
    return state, None


def settle(state: dict) -> tuple[dict, str]:
    p = state["pending"]
    return {"in": p["in"], "pending": None, "since": p["first"]}, "arrive" if p["in"] else "leave"


def verdict(place: dict, lat: float, lon: float) -> bool | None:
    d = distance_m(place, lat, lon)
    return True if d <= place["radius_m"] else False if d > LEAVE * place["radius_m"] else None
