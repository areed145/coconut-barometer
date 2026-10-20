"""Views, faves and comments over time; top photos, gainers, breakdowns."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, Query

from ..deps import session, store

router = APIRouter(prefix="/api/analytics", tags=["analytics"], dependencies=[Depends(session)])


def _since(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=days)).isoformat()


@router.get("/overview")
def overview(days: int = Query(30, ge=1, le=3650), db=Depends(store)):
    since = _since(days)
    return {"since": since, "totals": db.totals(), "series": db.daily_series(since),
            "top": db.top_photos(since, limit=12), "top_faved": db.top_photos(since, limit=12, by="favorites")}


@router.get("/gainers")
def gainers(days: int = Query(7, ge=1, le=365), limit: int = Query(20, ge=1, le=100), db=Depends(store)):
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    return db.gainers(days, today, limit)


@router.get("/breakdown")
def breakdown(by: Literal["album", "camera", "year", "media"] = "album", days: int = Query(30, ge=1, le=3650),
              limit: int = Query(30, ge=1, le=200), db=Depends(store)):
    return db.breakdown(by, _since(days), limit)


@router.get("/photos/{photo_id}")
def photo(photo_id: str, db=Depends(store)):
    return db.photo_timeline(photo_id)
