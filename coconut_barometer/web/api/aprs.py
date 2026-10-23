"""The APRS page: the tracked callsigns' newest positions, their tracks, and their recent packets; and the
igate's analytics. Public."""
from __future__ import annotations

import math
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query

from ...aprs.igate import bin_width, parse_location, weibull_fit
from ..deps import runtime, store
from .weather import _window

router = APIRouter(prefix="/api/aprs", tags=["aprs"])


def _tracked(rt, callsign: str) -> str:
    call = callsign.strip().upper()
    if call not in rt.aprs.callsigns():
        raise HTTPException(404, "not a tracked callsign")
    return call


@router.get("/stations")
def stations(rt=Depends(runtime), db=Depends(store)):
    """Each tracked callsign (Settings → APRS) with its newest position, and the APRS-IS connection's state."""
    return {"units": rt.cfg["display"]["units"], "now": int(time.time()), "stream": rt.aprs.hub.snapshot(),
            "callsigns": db.aprs_latest(rt.aprs.callsigns())}


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


# Grid cells for the igate map, (lat, lon) degrees: plain sizes, and Maidenhead subsquares and squares.
CELLS = {"0.02": (0.02, 0.02), "0.05": (0.05, 0.05), "0.1": (0.1, 0.1), "0.25": (0.25, 0.25), "0.5": (0.5, 0.5),
         "subsquare": (1 / 24, 1 / 12), "square": (1.0, 2.0)}


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
    try:
        fixed = parse_location(rt.cfg["aprs"]["igate_location"])
    except ValueError:
        fixed = None
    beacon = db.igate_placed(gate)
    home = ({"lat": fixed[0], "lon": fixed[1], "source": "setting"} if fixed
            else {"lat": beacon["lat"], "lon": beacon["lon"], "source": "beacon", "ts": beacon["ts"]} if beacon else None)
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
