"""Home's Highlights: the weather's streaks and month so far, the igate's farthest packet this month, and
this year's photos. Public (visitors get public photos only, and no positions: only the farthest station's
callsign and distance). One request for the whole card, cached until something new is stored."""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends

from ..deps import runtime, store, viewer
from .aprs import igate_home

router = APIRouter(prefix="/api", tags=["home"])
CACHE_SECONDS = 60
_cache: dict[tuple, tuple] = {}


def _weather(db) -> dict | None:
    """The first Tempest with observations, as on the Weather card."""
    for d in db.wx_stations():
        if d["last_ts"]:
            return {"device_id": d["device_id"], "name": d["name"], **db.wx_highlights(d["device_id"])}
    return None


def _igate(rt, db, month_start: int) -> dict | None:
    gate = rt.aprs.igate()
    home = igate_home(rt, db, gate) if gate else None
    if not home:
        return None
    far = db.igate_farthest((home["lat"], home["lon"]), gate, month_start, int(time.time()), True, limit=1)
    return {"igate": gate, "start": month_start, "farthest": {k: far[0][k] for k in ("callsign", "km", "ts")} if far else None}


@router.get("/highlights")
def highlights(rt=Depends(runtime), db=Depends(store), me=Depends(viewer)):
    key, now = (db.wx_freshness(), me is None), time.time()
    hit = _cache.get((rt.db_path, key[1]))
    if hit and hit[0] == key and now - hit[1] < CACHE_SECONDS:
        return hit[2]
    weather = _weather(db)
    zone = ZoneInfo(weather["timezone"]) if weather and weather.get("timezone") else ZoneInfo("UTC")
    local = datetime.fromtimestamp(now, zone)
    month_start = int(local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    out = {"units": rt.cfg["display"]["units"], "weather": weather if weather and weather.get("since") else None,
           "igate": _igate(rt, db, month_start), "photos": db.photo_highlights(local.year, month_start, public_only=me is None)}
    _cache[(rt.db_path, key[1])] = (key, now, out)
    return out
