"""The APRS page: the tracked callsigns' newest positions, their tracks, and their recent packets; and the
igate's analytics, each section its own request so the page loads them as it draws them. Public."""
from __future__ import annotations

import math
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from ...aprs.igate import MIN_FIT, MIN_SECTOR, bin_width, weibull_fit
from ...aprs.igate import home as where
from ...aprs.places import named
from ...aprs.trips import split
from ..deps import runtime, store, viewer
from .weather import _window

router = APIRouter(prefix="/api/aprs", tags=["aprs"])


def _tracked(rt, callsign: str) -> str:
    call = callsign.strip().upper()
    if call not in rt.aprs.callsigns():
        raise HTTPException(404, "not a tracked callsign")
    return call


@router.get("/stations")
def stations(rt=Depends(runtime), db=Depends(store), me=Depends(viewer)):
    """Each tracked callsign (Settings → APRS) with its newest position, and the APRS-IS connection's state;
    signed in, also the CWOP reporter's (Settings → APRS → Weather reports)."""
    out = {"units": rt.cfg["display"]["units"], "now": int(time.time()), "stream": rt.aprs.hub.snapshot(),
           "callsigns": db.aprs_latest(rt.aprs.callsigns())}
    if me is not None:
        out["cwop"] = rt.aprs.cwop_status()
    return out


@router.get("/track")
def track(callsign: str, start: int | None = None, end: int | None = None, hours: float = Query(24, gt=0, le=24 * 3650),
          rt=Depends(runtime), db=Depends(store)):
    """Positions in order, `start`/`end` (epoch seconds) or the last `hours`."""
    call = _tracked(rt, callsign)
    start, end = _window(start, end, hours)
    return {"callsign": call, "start": start, "end": end, "points": db.aprs_track(call, start, end)}


@router.get("/packets")
def packets(callsign: str, limit: int = Query(25, ge=1, le=500), rt=Depends(runtime), db=Depends(store)):
    """The newest position packets, newest first, raw text included."""
    call = _tracked(rt, callsign)
    return {"callsign": call, "packets": db.aprs_packets(call, limit)}


_trips: dict[tuple, dict] = {}      # a few recent splits: the page asks again every minute


@router.get("/trips")
def trips(callsign: str, start: int | None = None, end: int | None = None, hours: float = Query(24 * 7, gt=0, le=24 * 365),
          rt=Depends(runtime), db=Depends(store), me=Depends(viewer)):
    """A tracked callsign's trips over the window (#120), newest first, with the stops between them and the gaps
    where nothing was heard (aprs/trips.py). Stops and the ends of trips are named after Settings → APRS → Places
    for a signed-in viewer only. A trip's positions are /track over its start and end."""
    call = _tracked(rt, callsign)
    start, end = _window(start, end, hours)
    a = rt.cfg["aprs"]
    points = db.aprs_track(call, start, end)
    key = (rt.db_path, call, start // 60, end // 60, len(points), points[-1]["ts"] if points else 0, me is None,
           a["trip_stop_minutes"], a["trip_gap_minutes"], repr(a.get("places")))
    if key not in _trips:
        if len(_trips) > 32:
            _trips.clear()
        name = (lambda lat, lon: named(rt.cfg, lat, lon)) if me is not None else (lambda lat, lon: None)
        out = split(points, a["trip_stop_minutes"] * 60, a["trip_gap_minutes"] * 60, name)
        _trips[key] = {k: v[::-1] for k, v in out.items()}
    return {"callsign": call, "units": rt.cfg["display"]["units"], "start": start, "end": end, "positions": len(points),
            "first_ts": points[0]["ts"] if points else None, **_trips[key]}


# Grid cells for the igate map, (lat, lon) degrees: plain sizes, and Maidenhead subsquares and squares.
CELLS = {"0.02": (0.02, 0.02), "0.05": (0.05, 0.05), "0.1": (0.1, 0.1), "0.25": (0.25, 0.25), "0.5": (0.5, 0.5),
         "subsquare": (1 / 24, 1 / 12), "square": (1.0, 2.0)}


def igate_home(rt, db, gate: str) -> dict | None:
    """Where the igate is, for distances (igate.home)."""
    return where(rt.cfg, db, gate)


@router.get("/igate")
def igate(start: int | None = None, end: int | None = None, hours: float = Query(24 * 30, gt=0, le=24 * 3650),
          direct: bool = True, count: str = Query("packets", pattern="^(packets|stations)$"),
          cell: str = Query("0.1", pattern="^(" + "|".join(re.escape(c) for c in CELLS) + ")$"),
          rt=Depends(runtime), db=Depends(store)):
    """The igate in Settings → APRS: the packets APRS-IS credited it with over the window, counted on a lat/lon
    grid, and how far they came from (`direct`: heard without a digipeater only), binned, with a Weibull fit
    whose 90% distance is the effective range. `count=stations` bins each station once, at its farthest."""
    gate = rt.aprs.igate()
    start, end = _window(start, end, hours)
    out = {"igate": gate, "units": rt.cfg["display"]["units"], "now": int(time.time()), "start": start, "end": end,
           "stream": rt.aprs.hub.snapshot()}
    if not gate:
        return out
    dlat, dlon = CELLS[cell]
    rows = db.igate_cells(gate, start, end, dlat, dlon, direct)
    out |= {"totals": db.igate_totals(gate, start, end),
            "cells": {"dlat": dlat, "dlon": dlon, "rows": [[r["lat"], r["lon"], r["n"], r["stations"]] for r in rows]}}
    home = igate_home(rt, db, gate)
    out["home"] = home
    if not home:
        return out
    at, per_station = (home["lat"], home["lon"]), count == "stations"
    q = db.igate_quantiles(at, gate, start, end, direct, per_station)
    dist = {"count": count, "n": q["n"], "p50_km": q["p50"], "p90_km": q["p90"], "max_km": q["max"], "fit": None}
    if q["n"]:
        width = bin_width(q["p99"])
        nbins = max(1, math.ceil(q["p99"] / width + 1e-9))
        hist = db.igate_histogram(at, gate, start, end, direct, per_station, width)
        counts = [hist.get(i, 0) for i in range(nbins)]
        dist |= {"bin_km": width, "counts": counts, "beyond": q["n"] - sum(counts),
                 "fit": weibull_fit(width, counts, q["n"])}
    out |= {"distance": dist, "farthest": db.igate_farthest(at, gate, start, end, direct)}
    return out


def _utc_offset(db, at: int) -> int:
    """Seconds east of UTC at `at`, in the first weather station's zone (as Home's month), for local days."""
    zone = next((s["timezone"] for s in db.wx_stations() if s.get("timezone")), None)
    return int(datetime.fromtimestamp(at, ZoneInfo(zone or "UTC")).utcoffset().total_seconds())


@router.get("/igate/stations")
def igate_stations(start: int | None = None, end: int | None = None, hours: float = Query(24 * 30, gt=0, le=24 * 3650),
                   rt=Depends(runtime), db=Depends(store)):
    """Who the igate heard over the window (#118): each station with its type, whether it's new or a regular, and
    how far it usually is; the count of each type; and stations and new stations per day (or week)."""
    gate = rt.aprs.igate()
    start, end = _window(start, end, hours)
    out = {"igate": gate, "units": rt.cfg["display"]["units"], "start": start, "end": end}
    if not gate:
        return out
    home = igate_home(rt, db, gate)
    return out | db.igate_stations((home["lat"], home["lon"]) if home else None, gate, start, end, _utc_offset(db, end))


@router.get("/igate/sectors")
def igate_sectors(start: int | None = None, end: int | None = None, hours: float = Query(24 * 30, gt=0, le=24 * 3650),
                  direct: bool = True, sectors: int = Query(8, ge=8, le=16, multiple_of=8),
                  rt=Depends(runtime), db=Depends(store)):
    """Range by direction (#115): the packets with a position, by bearing from the igate in 8 or 16 sectors (the
    first centred on north), each with its own Weibull fit; a sector with fewer than MIN_SECTOR has no fit."""
    gate = rt.aprs.igate()
    start, end = _window(start, end, hours)
    out = {"igate": gate, "start": start, "end": end, "sectors": sectors, "min_packets": MIN_SECTOR, "rows": []}
    home = igate_home(rt, db, gate) if gate else None
    if not home:
        return out
    width = 360 / sectors
    for r in db.igate_sectors((home["lat"], home["lon"]), gate, start, end, direct, sectors):
        fit = weibull_fit(r["bin_km"], r["counts"], r["n"]) if r["n"] >= MIN_SECTOR else None
        out["rows"].append({"sector": r["sector"], "from_deg": (r["sector"] - 0.5) * width % 360, "to_deg": (r["sector"] + 0.5) * width % 360,
                            "n": r["n"], "p50_km": r["p50_km"],
                            "fit": {k: fit[k] for k in ("k", "r2", "r50_km", "effective_km")} if fit else None})
    return out


@router.get("/igate/digis")
def igate_digis(start: int | None = None, end: int | None = None, hours: float = Query(24 * 30, gt=0, le=24 * 3650),
                rt=Depends(runtime), db=Depends(store)):
    """The digipeaters the igate's packets came through over the window (#117), and the links between them."""
    gate = rt.aprs.igate()
    start, end = _window(start, end, hours)
    out = {"igate": gate, "start": start, "end": end}
    return out | db.igate_digis(gate, start, end) if gate else out


@router.get("/igate/long")
def igate_long(start: int | None = None, end: int | None = None, hours: float = Query(24 * 30, gt=0, le=24 * 3650),
               rt=Depends(runtime), db=Depends(store)):
    """The long-distance log (#116): the stations heard farthest direct over the window, and ever, each at its
    farthest believable packet, and what was left out as unbelievable."""
    gate = rt.aprs.igate()
    start, end = _window(start, end, hours)
    out = {"igate": gate, "units": rt.cfg["display"]["units"], "start": start, "end": end}
    at = igate_home(rt, db, gate) if gate else None
    if not at:
        return out
    at = (at["lat"], at["lon"])
    return out | {"window": db.igate_long(at, gate, start, end), "ever": db.igate_long(at, gate, 0, end)}


@router.get("/igate/range")
def igate_range(start: int | None = None, end: int | None = None, hours: float = Query(24 * 30, gt=0, le=24 * 3650),
                step: str = Query("day", pattern="^(day|hour)$"), rt=Depends(runtime), db=Depends(store)):
    """Range over time against the weather (#116), per local day or hour (hours for windows up to 31 days):
    effective range (each bucket's own Weibull fit, with MIN_FIT direct packets or more), the 99th-percentile
    direct distance, the stations heard (range rises with how many are sending), and the first weather station's
    sea-level pressure, temperature and dew point; per day also how far it cooled overnight."""
    gate = rt.aprs.igate()
    start, end = _window(start, end, hours)
    if step == "hour" and end - start > 31 * 86400:
        raise HTTPException(422, "hourly is for windows up to 31 days")
    seconds, offset = (86400 if step == "day" else 3600), _utc_offset(db, end)
    out = {"igate": gate, "units": rt.cfg["display"]["units"], "start": start, "end": end, "step": step, "min_packets": MIN_FIT,
           "t": []}
    at = igate_home(rt, db, gate) if gate else None
    if not at:
        return out
    series = db.igate_range_series((at["lat"], at["lon"]), gate, start, end, seconds, offset)
    station = next((s for s in db.wx_stations() if s["last_ts"]), None)
    wx = db.wx_buckets(station["device_id"], start, end, seconds, offset) if station else {}
    drop = db.wx_overnight(station["device_id"], start, end, offset) if station and step == "day" else {}
    ts = sorted(series)
    fits = {t: weibull_fit(series[t]["bin_km"], series[t]["counts"], series[t]["n"]) if series[t]["n"] >= MIN_FIT else None for t in ts}
    col = lambda f: [f(t) for t in ts]
    return out | {"t": ts, "station": station and station["name"],
                  "effective_km": col(lambda t: fits[t] and fits[t]["effective_km"]), "p99_km": col(lambda t: series[t]["p99_km"]),
                  "direct": col(lambda t: series[t]["n"]), "stations": col(lambda t: series[t]["stations"]),
                  "pressure": col(lambda t: (wx.get(t) or {}).get("pressure")), "temp": col(lambda t: (wx.get(t) or {}).get("temp")),
                  "dew_point": col(lambda t: (wx.get(t) or {}).get("dew_point")),
                  "overnight_drop": col(lambda t: drop.get(t)) if step == "day" else None}
