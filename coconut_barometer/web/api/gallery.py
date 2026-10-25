"""Photostream, albums and photo detail. Public: visitors who aren't signed in see only public photos."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ...planner import photo_page as flickr_url
from ...store.gallery import PRESETS, SETTINGS
from ..deps import runtime, store, viewer

router = APIRouter(prefix="/api", tags=["gallery"])
Sort = Literal["newest", "oldest", "taken", "taken_asc", "views", "faves", "comments", "title"]


def filters(q: str = Query("", max_length=100), media: Literal["photo", "video"] | None = None,
            album: str | None = Query(None, max_length=40), tag: str = Query("", max_length=100),
            year: int | None = Query(None, ge=1800, le=3000), camera: str = Query("", max_length=200),
            on: str = Query("", pattern=r"^(|(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01]))$"),
            day: str = Query("", pattern=r"^(|\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01]))$"),
            near: str = Query("", pattern=r"^(|-?\d{1,2}(\.\d+)?,-?\d{1,3}(\.\d+)?)$"),
            lens: str = Query("", max_length=200), trip: str = Query("", max_length=40),
            setting: str = Query("", pattern=r"^(|[a-z]+:\d+(\.\d+)?(e-?\d+)?-(\d+(\.\d+)?(e-?\d+)?)?)$", max_length=60),
            is_: str = Query("", alias="is", max_length=300), me=Depends(viewer)) -> dict:
    """The Photos page's filters: album, tag, year, `on` this day of any year ("MM-DD"), the `day` taken
    ("YYYY-MM-DD"), `near` a point ("lat,lon"), camera, lens, an EXIF `setting` from low up to (not including) high
    ("focal:24-28", "iso:6400-": see SETTINGS), a trip, search, and presets (?is=untitled,no_album means both)."""
    presets = [p for p in is_.split(",") if p]
    unknown = [p for p in presets if p not in PRESETS or (me is None and PRESETS[p][1] == "Status")]
    if unknown:
        raise HTTPException(422, f"unknown preset: {', '.join(unknown)}")
    point = tuple(float(x) for x in near.split(",")) if near else None
    if point and not (-90 <= point[0] <= 90 and -180 <= point[1] <= 180):
        raise HTTPException(422, "near: no such place")
    exif = None
    if setting:
        name, _, span = setting.partition(":")
        low, _, high = span.partition("-")
        if name not in SETTINGS:
            raise HTTPException(422, f"setting: no such setting {name!r}")
        exif = (name, float(low), float(high) if high else None)
    return dict(album_id=album, q=q, media=media, tag=tag, year=year, camera=camera, presets=presets,
                public_only=me is None, on=on, day=day, near=point, lens=lens, setting=exif, trip=trip)


@router.get("/photos")
def photos(sort: Sort = "newest", limit: int = Query(60, ge=1, le=200), offset: int = Query(0, ge=0),
           f: dict = Depends(filters), db=Depends(store)):
    """The Photos page and every filtered view of it."""
    return db.photo_page(sort=sort, limit=limit, offset=offset, **f)


@router.get("/photo-months")
def photo_months(sort: Sort = "newest", f: dict = Depends(filters), db=Depends(store)):
    """Photos per month, in the sort's order, for the timeline (#126): their running total is each month's offset."""
    return db.photo_months(sort=sort, **f)


@router.get("/theme")
def theme(rt=Depends(runtime)):
    """The color gradient (Settings → Display → Colors) for the map and the charts."""
    return {"colors": gradient(rt.cfg)}


def gradient(cfg: dict) -> list[str]:
    """Low, medium, high."""
    return [cfg["theme"][f"color_{k}"] for k in ("low", "mid", "high")]


_PRESET_COUNTS: dict[tuple[str, bool], tuple[float, list]] = {}    # (database path, public only) -> (when, counts)


@router.get("/photo-presets")
def photo_presets(db=Depends(store), me=Depends(viewer)):
    """The presets, grouped, with how many photos each matches. Counted at most once a
    minute (the count takes most of a second on a large library)."""
    import time

    key = (db.path, me is None)
    at, counts = _PRESET_COUNTS.get(key, (0.0, None))
    if counts is None or time.time() - at > 60:
        counts = db.preset_counts(public_only=me is None)
        _PRESET_COUNTS[key] = (time.time(), counts)
    return counts


_GEAR: dict[tuple[str, bool], tuple[float, dict]] = {}     # (database path, public only) -> (when, gear)


@router.get("/gear")
def gear(db=Depends(store), me=Depends(viewer)):
    """Cameras, lenses and settings (the Gear page). Worked out at most once a minute: it reads every photo's EXIF."""
    import time

    key = (db.path, me is None)
    at, got = _GEAR.get(key, (0.0, None))
    if got is None or time.time() - at > 60:
        got = db.gear(public_only=me is None)
        _GEAR[key] = (time.time(), got)
    return got


@router.get("/photo-years")
def photo_years(db=Depends(store), me=Depends(viewer)):
    """Years taken, with counts, for the Photos page's year filter."""
    return db.years_taken(public_only=me is None)


@router.get("/photos/{photo_id}")
def photo(photo_id: str, db=Depends(store), me=Depends(viewer)):
    p = db.photo_detail(photo_id, public_only=me is None)
    if not p:
        raise HTTPException(404, "no such photo")
    return {**p, "url": flickr_url(db.get_meta("nsid", "me"), photo_id), "engagement": db.engagement(photo_id),
            "trip": db.photo_trip(photo_id, public_only=me is None)}


@router.get("/photos/{photo_id}/related")
def related(photo_id: str, db=Depends(store), me=Depends(viewer)):
    """Photos taken nearby and on the same day, for the lightbox."""
    r = db.related(photo_id, public_only=me is None)
    if r is None:
        raise HTTPException(404, "no such photo")
    return r


@router.get("/trips")
def trips(rt=Depends(runtime), db=Depends(store), me=Depends(viewer)):
    """Trips (#125): photos grouped by when and where they were taken, newest first. Built here the first time, and
    again when the photos or the settings have changed (most often it's the sync that does it)."""
    from ...trips import ensure

    ensure(rt.cfg, db)
    return db.trip_list(public_only=me is None)


@router.get("/trips/{trip_id}")
def trip(trip_id: str, db=Depends(store), me=Depends(viewer)):
    t = db.trip_list(public_only=me is None, trip_id=trip_id)
    if not t:
        raise HTTPException(404, "no such trip")
    return t[0]


@router.get("/albums")
def albums(db=Depends(store), me=Depends(viewer)):
    return db.album_list(public_only=me is None)


@router.get("/albums/{album_id}")
def album(album_id: str, sort: Sort = "newest", limit: int = Query(60, ge=1, le=200), offset: int = Query(0, ge=0),
          db=Depends(store), me=Depends(viewer)):
    a = db.album(album_id, public_only=me is None)
    if not a:
        raise HTTPException(404, "no such album")
    return {**a, **db.photo_page(album_id=album_id, sort=sort, limit=limit, offset=offset, public_only=me is None)}
