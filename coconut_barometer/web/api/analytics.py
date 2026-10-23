"""The Analytics tabs: overview, photos, tags, places, gear, audience, albums and groups,
and whether this tool's changes helped (see insights.py)."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, Query

from ... import insights as I
from ..deps import session, store

router = APIRouter(prefix="/api/analytics", tags=["analytics"], dependencies=[Depends(session)])


def _with_sizes(db, rows: list[dict]) -> list[dict]:
    cards = db.cards([r["photo_id"] for r in rows])
    return [{**r, "sizes": (cards.get(r["photo_id"]) or {}).get("sizes", {})} for r in rows]


def _since(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=days)).isoformat()


@router.get("/overview")
def overview(days: int = Query(30, ge=1, le=3650), db=Depends(store)):
    """The period's daily series, the period before it (for "vs before"), and highlights. The totals
    are of whole days: today's views so far are in the series (partial) but not in them."""
    since, before = _since(days), _since(2 * days)
    series = db.daily_series(before)
    current = [r for r in series if r["day"] >= since]
    previous = [r for r in series if r["day"] < since]
    total = lambda rows, k: sum(r[k] or 0 for r in rows if not r["partial"])
    recent_faves = [dict(r) for r in db.db.execute(
        "SELECT f.photo_id, p.title, COALESCE(NULLIF(f.real_name, ''), f.username) AS who, f.date_faved AS \"at\" FROM faves f "
        "JOIN photos p ON p.id = f.photo_id ORDER BY f.date_faved DESC LIMIT 8")]
    recent_comments = [dict(r) for r in db.db.execute(
        "SELECT c.photo_id, p.title, COALESCE(NULLIF(c.real_name, ''), c.author_name) AS who, c.date_created AS \"at\", c.text "
        "FROM comments c JOIN photos p ON p.id = c.photo_id ORDER BY c.date_created DESC LIMIT 5")]
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    return {"since": since, "totals": db.totals(), "series": current,
            "period": {k: total(current, k) for k in ("views", "faves", "comments")},
            "previous": {k: total(previous, k) for k in ("views", "faves", "comments")} if previous else None,
            "highlights": {"gaining": _with_sizes(db, db.gainers(min(days, 7), today, 3)),
                           "faves": _with_sizes(db, recent_faves), "comments": _with_sizes(db, recent_comments)},
            "days_collected": I._days_collected(db)}


@router.get("/photos")
def photo_lists(kind: Literal["views", "faves", "comments", "engagement", "sleepers", "overlooked", "gaining"] = "views",
                days: int = Query(30, ge=1, le=3650), limit: int = Query(24, ge=1, le=100),
                min_views: int = Query(100, ge=1, le=100000), db=Depends(store)):
    if kind == "gaining":
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        rows = [{**r, "value": r["gain"]} for r in db.gainers(min(days, 90), today, limit)]
    else:
        rows = I.photo_lists(db, kind, days, limit, min_views)
    return _with_sizes(db, rows)


@router.get("/tags")
def tags(source: Literal["all", "yours", "added"] = "all", hide: str = Query("date,common", max_length=60),
         min_photos: int = Query(3, ge=1, le=1000), sort: str = Query("photos", max_length=20),
         limit: int = Query(400, ge=1, le=2000), db=Depends(store)):
    """hide: comma-separated kinds to leave out (date, place, camera, common)."""
    kinds = tuple(k for k in hide.split(",") if k in ("date", "place", "camera", "common"))
    return I.tag_table(db, source, kinds, min_photos, limit, sort)


@router.get("/tags/{tag}")
def tag(tag: str, db=Depends(store)):
    out = I.tag_detail(db, tag[:100])
    cards = db.cards(out["top"])
    out["top"] = [cards[i] for i in out["top"] if i in cards]
    return out


@router.get("/places")
def places(level: Literal["country", "state", "city"] = "country", db=Depends(store)):
    return I.places(db, level)


@router.get("/gear")
def gear(db=Depends(store)):
    return I.gear(db)


@router.get("/audience")
def audience(db=Depends(store)):
    return I.audience(db)


@router.get("/groups")
def albums_and_groups(db=Depends(store)):
    return I.albums_and_groups(db)


@router.get("/impact")
def impact(window: int = Query(14, ge=3, le=60), db=Depends(store)):
    return I.impact(db, window)


@router.get("/gainers")
def gainers(days: int = Query(7, ge=1, le=365), limit: int = Query(20, ge=1, le=100), db=Depends(store)):
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    return _with_sizes(db, db.gainers(days, today, limit))


@router.get("/breakdown")
def breakdown(by: Literal["album", "camera", "year", "media"] = "album", days: int = Query(30, ge=1, le=3650),
              limit: int = Query(30, ge=1, le=200), db=Depends(store)):
    return db.breakdown(by, _since(days), limit)


@router.get("/photos/{photo_id}")
def photo(photo_id: str, db=Depends(store)):
    return db.photo_timeline(photo_id)
