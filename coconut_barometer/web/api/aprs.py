"""The APRS page: the tracked callsigns' newest positions, their tracks, and their recent packets."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import runtime, session, store
from .weather import _window

router = APIRouter(prefix="/api/aprs", tags=["aprs"], dependencies=[Depends(session)])


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
