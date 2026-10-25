"""A tracked callsign's positions as trips (#120): where it went between stops.

Positions within STOP_M of where a stay began are one stay; a stay of aprs.trip_stop_minutes or more is a stop
(shorter ones, a traffic light, are part of the trip). Positions far apart with nothing heard for
aprs.trip_gap_minutes between them are a gap: the trip ends there and the next begins, without a stop between.
A trip runs from the last position of one stop (or gap) to the first of the next.

Beacons are sparse (smart beaconing sends more on turns and fewer on straight roads), so a trip's distance is the
sum of straight lines between positions, an underestimate; a position reached faster than JUMP_KMH is a bad
one, and measured past. Speed is what the tracker reported where it did, else the step's own. Elevation gain counts rises of
CLIMB_M or more, so altitude noise doesn't add up."""
from __future__ import annotations

from ..geo import haversine_km

STOP_M = 100
MOVING_MS = 1.0          # a step slower than this (m/s) is standing, not moving
JUMP_KMH = 300
CLIMB_M = 3
MIN_TRIP_KM = 0.2


def _km(a: dict, b: dict) -> float:
    return haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])


def stays(points: list[dict], gap_s: int) -> list[tuple[str, int, int]]:
    """The positions as runs: ("stay", i, j) for points i..j within STOP_M of point i, and ("gap", i, j) between
    two positions (j = i + 1) far apart with more than gap_s between them."""
    out, i = [], 0
    while i < len(points):
        j = i
        while j + 1 < len(points) and _km(points[i], points[j + 1]) * 1000 <= STOP_M:
            j += 1
        out.append(("stay", i, j))
        if j + 1 < len(points) and points[j + 1]["ts"] - points[j]["ts"] > gap_s:
            out.append(("gap", j, j + 1))
        i = j + 1
    return out


def stats(points: list[dict]) -> dict:
    """Distance (km), moving time (s), average and top speed (m/s), elevation gain (m), packets and how often."""
    km = moving = 0.0
    top, climb, base = 0.0, 0.0, None
    a = points[0]
    for b in points[1:]:
        dt, d = b["ts"] - a["ts"], _km(a, b)
        if dt <= 0 or d / dt * 3600 > JUMP_KMH:
            continue                                # a bad position: measured past, from the last good one
        km += d
        step = d * 1000 / dt
        if step >= MOVING_MS or (b.get("speed") or 0) >= MOVING_MS:
            moving += dt
        top = max(top, b.get("speed") if b.get("speed") is not None else step)
        a = b
    for p in points:
        if p.get("altitude") is None:
            continue
        if base is None or p["altitude"] < base:
            base = p["altitude"]
        elif p["altitude"] - base >= CLIMB_M:
            climb, base = climb + p["altitude"] - base, p["altitude"]
    span = points[-1]["ts"] - points[0]["ts"]
    return {"km": round(km, 3), "moving_s": int(moving), "avg_ms": round(km * 1000 / moving, 2) if moving else None,
            "top_ms": round(top, 2), "climb_m": round(climb) if any(p.get("altitude") is not None for p in points) else None,
            "packets": len(points), "every_s": round(span / (len(points) - 1)) if len(points) > 1 else None}


def split(points: list[dict], stop_s: int, gap_s: int, name=lambda lat, lon: None) -> dict:
    """{trips, stops, gaps} from positions in time order (ts, lat, lon, altitude, speed). `name(lat, lon)` names
    a place (aprs/places.named) for the ends of trips and for stops."""
    runs = stays(points, gap_s)
    stops, gaps, breaks = [], [], []          # breaks: (last point before, first point after) of each stop or gap
    for kind, i, j in runs:
        if kind == "gap":
            gaps.append({"from_ts": points[i]["ts"], "to_ts": points[j]["ts"], "km": round(_km(points[i], points[j]), 2)})
            breaks.append((i, j))
        elif points[j]["ts"] - points[i]["ts"] >= stop_s:
            p = points[i]
            stops.append({"lat": p["lat"], "lon": p["lon"], "from_ts": p["ts"], "to_ts": points[j]["ts"],
                          "place": name(p["lat"], p["lon"])})
            breaks.append((i, j))
    trips = []
    starts = [0] + [b[1] for b in breaks]     # a trip leaves from a stop's last point, or the far side of a gap
    ends = [b[0] for b in breaks] + [len(points) - 1]
    for s, e in zip(starts, ends):
        if e <= s:
            continue
        leg = points[s:e + 1]
        st = stats(leg)
        if st["km"] < MIN_TRIP_KM:
            continue
        a, b = leg[0], leg[-1]
        trips.append({"start_ts": a["ts"], "end_ts": b["ts"], "from": [a["lat"], a["lon"]], "to": [b["lat"], b["lon"]],
                      "from_place": name(a["lat"], a["lon"]), "to_place": name(b["lat"], b["lon"]), **st})
    return {"trips": trips, "stops": stops, "gaps": gaps}
