"""The Weather pages: stations, the live view, history for charts, a wind rose, the radar/satellite map.
Public (read-only); starting a sync needs a login."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query

from ...weather.imagery import Imagery
from ..deps import runtime, session, store

router = APIRouter(prefix="/api/wx", tags=["weather"])


def _units(rt) -> str:
    return rt.cfg["display"]["units"]


@router.get("/stations")
def stations(rt=Depends(runtime), db=Depends(store)):
    """The Tempest stations known so far, with how much history each has, and the stream's state."""
    return {"configured": rt.weather.token() is not None, "units": _units(rt),
            "backfill_days": rt.cfg["weatherflow"]["backfill_days"],
            "stations": db.wx_stations(), "stream": rt.weather.hub.snapshot()["stream"]}


@router.post("/sync", dependencies=[Depends(session)])
def sync(rt=Depends(runtime)):
    """Queue a REST sync: refresh the stations, fetch missing history. Shows in the Queue."""
    return rt.review.wx_sync()


# The stations part of /live, per database: rebuilt when a new observation or event is stored (or after a
# minute, for anything else a sync changes). Every Live viewer polls every 3 s; observations come once a minute.
_live_cache: dict[str, tuple] = {}
LIVE_CACHE_SECONDS = 60


def _latest(rt, db) -> list[dict]:
    key, now = db.wx_freshness(), time.time()
    hit = _live_cache.get(rt.db_path)
    if hit and hit[0] == key and now - hit[1] < LIVE_CACHE_SECONDS:
        return hit[2]
    stations = [s for s in (db.wx_latest(d["device_id"]) for d in db.wx_devices()) if s]
    _live_cache[rt.db_path] = (key, now, stations)
    return stations


@router.get("/live")
def live(rt=Depends(runtime), db=Depends(store)):
    """Each station's newest observation plus the 3-second wind from the stream. Poll this every few seconds."""
    snap = rt.weather.hub.snapshot()
    return {"units": _units(rt), "now": int(time.time()), "stream": snap["stream"], "rapid": snap["rapid"],
            "events": snap["events"], "stations": _latest(rt, db)}


def _window(start, end, hours):
    end = end or int(time.time())
    start = start if start is not None else int(end - hours * 3600)
    if start >= end:
        raise HTTPException(422, "start must be before end")
    return start, end


@router.get("/history")
def history(device: list[int] = Query(...), start: int | None = None, end: int | None = None,
            hours: float = Query(24, gt=0, le=24 * 3650), db=Depends(store), rt=Depends(runtime)):
    """Charts: one series set per device, on shared timestamps. `start`/`end` (epoch seconds), or the last `hours`."""
    start, end = _window(start, end, hours)
    if not set(device) <= {d["device_id"] for d in db.wx_devices()}:
        raise HTTPException(404, "unknown device")
    return {"units": _units(rt), "start": start, "end": end, **db.wx_history(device, start, end)}


@router.get("/lightning")
def lightning(device: int, start: int | None = None, end: int | None = None, hours: float = Query(24, gt=0, le=24 * 3650),
              db=Depends(store)):
    """Lightning strike counts in 30-minute bins by distance (km), for the heatmap."""
    start, end = _window(start, end, hours)
    if device not in {d["device_id"] for d in db.wx_devices()}:
        raise HTTPException(404, "unknown device")
    return {"start": start, "end": end, **db.wx_lightning(device, start, end)}


@router.get("/days")
def days(device: int, start: int | None = None, end: int | None = None, hours: float = Query(24 * 7, gt=0, le=24 * 3650),
         db=Depends(store)):
    """Temperature by local day: the daily low/high/mean and the 24 hourly means, for the band and the heatmap."""
    start, end = _window(start, end, hours)
    if device not in {d["device_id"] for d in db.wx_devices()}:
        raise HTTPException(404, "unknown device")
    return {"start": start, "end": end, **db.wx_days(device, start, end)}


_imagery = Imagery()


def imagery() -> Imagery:
    return _imagery


@router.get("/map")
def map_layers(db=Depends(store), rt=Depends(runtime), im: Imagery = Depends(imagery)):
    """The Map tab: where the stations are, and the radar and satellite frames available now."""
    return {"units": _units(rt), "now": int(time.time()),
            "stations": [{k: s[k] for k in ("device_id", "name", "lat", "lon")} for s in db.wx_stations() if s["lat"] is not None],
            **im.frames()}


@router.get("/rose")
def rose(device: int, start: int | None = None, end: int | None = None, hours: float = Query(24 * 7, gt=0, le=24 * 3650),
         part: str | None = Query(None, pattern="^(night|morning|afternoon|evening)$"), db=Depends(store)):
    """Wind by direction and speed band; `part` keeps one quarter of the station's local day."""
    start, end = _window(start, end, hours)
    if device not in {d["device_id"] for d in db.wx_devices()}:
        raise HTTPException(404, "unknown device")
    return db.wx_wind_rose(device, start, end, part)
