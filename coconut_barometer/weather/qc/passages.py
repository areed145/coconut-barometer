"""Station QC's front passages (#111): when a WPC front (fronts.py, every 3 h, kept by sampler.py) crosses a station,
whether the station recorded what that front brings.

A passage: between two analyses at most MAX_GAP apart, the station goes from ahead of a front of one type (on its
left, walking its points in order: the side it's moving toward) to behind it, both within CROSS_KM. A stationary
front or a trough has no "ahead", so either way. Its time is interpolated by the two distances. Then the station's
readings within WINDOW of it, as STEP means, are searched for the signatures, each the strongest change between the
HALF before and the HALF after any point:

    wind     the direction shifting SHIFT_DEG or more (veering, clockwise, behind a cold or occluded front)
    cooler   the dewpoint or the temperature falling DROP °C or more
    warmer   either rising RISE °C or more
    pressure the lowest pressure, then a rise of PRESSURE_RISE hPa or more

    cold, occluded       pressure, wind, cooler: agrees with 2 of them
    warm                 wind, warmer: 1
    stationary, trough   wind

Analyzed fronts are drawn by hand every 3 hours, so a passage that doesn't agree is a weak sign: this check warns,
never fails. Each passage is kept with what was found, for History's "Cold front 14:20" labels (#113)."""
from __future__ import annotations

import math

import numpy as np

from .internal import obs

KM_PER_DEG = 111.2
CROSS_KM, MAX_GAP = 300.0, 6 * 3600
WINDOW, STEP, HALF = 3 * 3600, 1800, 3          # HALF in STEPs: 1.5 h
SHIFT_DEG, DROP, RISE, PRESSURE_RISE = 30.0, 1.5, 1.0, 1.0
MIN_READINGS = 10                                # a STEP with fewer readings is left out
MIN_PASSAGES = 3
EXPECT = {"cold": (("pressure", "wind", "cooler"), 2), "occluded": (("pressure", "wind", "cooler"), 2),
          "warm": (("wind", "warmer"), 1), "stationary": (("wind",), 1), "trough": (("wind",), 1)}
MOVING = {"cold", "warm", "occluded"}


def side(points, lat0: float, lon0: float) -> tuple[float, int] | None:
    """(km, +1 ahead (left of the line) or -1 behind) of the place from a front's nearest segment; None if it's
    past either end of the front, or the front is a single point."""
    c = math.cos(math.radians(lat0))
    xy = [((lon - lon0) * c * KM_PER_DEG, (lat - lat0) * KM_PER_DEG) for lat, lon in points]
    best = None
    for (ax, ay), (bx, by) in zip(xy, xy[1:]):
        dx, dy = bx - ax, by - ay
        length = dx * dx + dy * dy
        if not length:
            continue
        t = (-ax * dx - ay * dy) / length
        if t < 0 or t > 1:
            continue
        km = math.hypot(ax + t * dx, ay + t * dy)
        if best is None or km < best[0]:
            best = (km, 1 if dx * -ay - dy * -ax > 0 else -1)
    return best


def crossings(analyses: list[tuple[int, dict]], lat: float, lon: float) -> list[dict]:
    """Each front passage at the place: {ts, type}, oldest first."""
    out = []
    for (v1, a1), (v2, a2) in zip(analyses, analyses[1:]):
        if v2 - v1 > MAX_GAP:
            continue
        for kind in EXPECT:
            near = []
            for a in (a1, a2):
                s = [x for x in (side(f["points"], lat, lon) for f in a["fronts"] if f["type"] == kind) if x]
                near.append(min(s) if s else None)
            (n1, n2) = near
            if not n1 or not n2 or n1[0] > CROSS_KM or n2[0] > CROSS_KM:
                continue
            if (n1[1], n2[1]) == (1, -1) or (kind not in MOVING and n1[1] != n2[1]):
                out.append({"ts": int(v1 + (v2 - v1) * n1[0] / (n1[0] + n2[0])), "type": kind})
    return sorted(out, key=lambda p: p["ts"])


def signatures(a: dict, t: int) -> dict | None:
    """The strongest changes in the readings around `t` (a: internal.obs's arrays); None without enough readings."""
    edges = np.arange(t - WINDOW - HALF * STEP, t + WINDOW + HALF * STEP + 1, STEP)
    k = np.searchsorted(edges, a["ts"], side="right") - 1
    inside = (k >= 0) & (k < len(edges) - 1)
    n = len(edges) - 1
    count = np.bincount(k[inside], minlength=n)
    if (count >= MIN_READINGS).sum() < n // 2:
        return None

    def means(v):
        ok = inside & ~np.isnan(v)
        s = np.bincount(k[ok], weights=v[ok], minlength=n)
        c = np.bincount(k[ok], minlength=n)
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(c >= MIN_READINGS, s / np.maximum(c, 1), np.nan)

    rad = np.radians(a["wind_dir"])
    east, north = means(a["wind_avg"] * np.sin(rad)), means(a["wind_avg"] * np.cos(rad))
    temp, dew, pressure = means(a["temp"]), means(a["dew"]), means(a["pressure"])
    shift = cool = warm = None
    when = None
    for s in range(HALF, n - HALF + 1):
        b, f = slice(s - HALF, s), slice(s, s + HALF)
        with np.errstate(invalid="ignore"):
            eb, nb, ef, nf = np.nanmean(east[b]), np.nanmean(north[b]), np.nanmean(east[f]), np.nanmean(north[f])
            changes = [np.nanmean(x[f]) - np.nanmean(x[b]) for x in (temp, dew)]
        if min(math.hypot(eb, nb), math.hypot(ef, nf)) >= 1.0:          # wind enough on both sides to have a direction
            turn = (math.degrees(math.atan2(ef, nf) - math.atan2(eb, nb)) + 180) % 360 - 180
            if shift is None or abs(turn) > abs(shift):
                shift, when = turn, int(edges[s])
        changes = [c for c in changes if not math.isnan(c)]
        if changes:
            cool = min(cool, min(changes)) if cool is not None else min(changes)
            warm = max(warm, max(changes)) if warm is not None else max(changes)
    rise = None
    core = slice(HALF, n - HALF)                     # the lowest within WINDOW of t, then the highest after it
    if not np.isnan(pressure[core]).all():
        low = HALF + int(np.nanargmin(pressure[core]))
        after = pressure[low + 1:]
        if not np.isnan(after).all():
            rise = float(np.nanmax(after) - pressure[low])
    return {"wind_shift": None if shift is None else round(shift, 1), "cooler": None if cool is None else round(-cool, 2),
            "warmer": None if warm is None else round(warm, 2), "pressure_rise": None if rise is None else round(rise, 2),
            "when": when}


def found(kind: str, sig: dict) -> list[str]:
    veer = kind in ("cold", "occluded")
    shift = sig["wind_shift"]
    out = []
    if shift is not None and (shift >= SHIFT_DEG if veer else abs(shift) >= SHIFT_DEG):
        out.append("wind")
    if sig["cooler"] is not None and sig["cooler"] >= DROP:
        out.append("cooler")
    if sig["warmer"] is not None and sig["warmer"] >= RISE:
        out.append("warmer")
    if sig["pressure_rise"] is not None and sig["pressure_rise"] >= PRESSURE_RISE:
        out.append("pressure")
    return out


def fronts(store, station, start, end, cfg) -> dict:
    q = cfg["qc"]
    analyses = store.qc_front_analyses(start - MAX_GAP, end)
    a = obs(store, station, start, end)
    passages = []
    for p in crossings(analyses, station["lat"], station["lon"]):
        if not start <= p["ts"] <= end:
            continue
        sig = signatures(a, p["ts"])
        if sig is None:
            continue                                 # no readings then: not judged
        expected, need = EXPECT[p["type"]]
        got = [x for x in found(p["type"], sig) if x in expected]
        passages.append({**p, **sig, "found": got, "agrees": len(got) >= need})
    n, agree = len(passages), sum(p["agrees"] for p in passages)
    metrics = {"passages": passages, "n": n, "agree": agree, "show": [["front passages", n, "count"]]}
    if n < MIN_PASSAGES:
        return {"status": "insufficient", "metrics": metrics}
    rate = agree / n
    metrics["rate"] = round(rate, 3)
    metrics["show"] = [["of front passages as expected", rate * 100, "pct"], ["front passages", n, "count"]]
    return {"status": "warn" if rate < q["fronts_warn"] else "pass", "metrics": metrics}


SUITES = {"fronts": ("Front passages", fronts)}
