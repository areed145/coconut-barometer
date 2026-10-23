"""Photostream, albums and photo detail. Public: visitors who aren't signed in see only public photos."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ...planner import photo_page as flickr_url
from ...store.gallery import PRESETS
from ..deps import runtime, store, viewer

router = APIRouter(prefix="/api", tags=["gallery"])
Sort = Literal["newest", "oldest", "taken", "views", "faves", "comments", "title"]


@router.get("/photos")
def photos(q: str = Query("", max_length=100), sort: Sort = "newest", media: Literal["photo", "video"] | None = None,
           album: str | None = Query(None, max_length=40), tag: str = Query("", max_length=100),
           year: int | None = Query(None, ge=1800, le=3000), camera: str = Query("", max_length=200),
           on: str = Query("", pattern=r"^(|(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01]))$"),
           is_: str = Query("", alias="is", max_length=300),
           limit: int = Query(60, ge=1, le=200), offset: int = Query(0, ge=0), db=Depends(store), me=Depends(viewer)):
    """The Photos page and every filtered view of it (album, tag, year, `on` this day of any year
    ("MM-DD"), camera, search, and presets: ?is=untitled,no_album means both)."""
    presets = [p for p in is_.split(",") if p]
    unknown = [p for p in presets if p not in PRESETS or (me is None and PRESETS[p][1] == "Status")]
    if unknown:
        raise HTTPException(422, f"unknown preset: {', '.join(unknown)}")
    return db.photo_page(album_id=album, q=q, sort=sort, media=media, limit=limit, offset=offset, tag=tag,
                         year=year, camera=camera, presets=presets, public_only=me is None, on=on)


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


@router.get("/photo-years")
def photo_years(db=Depends(store), me=Depends(viewer)):
    """Years taken, with counts, for the Photos page's year filter."""
    return db.years_taken(public_only=me is None)


@router.get("/photos/{photo_id}")
def photo(photo_id: str, db=Depends(store), me=Depends(viewer)):
    p = db.photo_detail(photo_id, public_only=me is None)
    if not p:
        raise HTTPException(404, "no such photo")
    return {**p, "url": flickr_url(db.get_meta("nsid", "me"), photo_id), "engagement": db.engagement(photo_id)}


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
