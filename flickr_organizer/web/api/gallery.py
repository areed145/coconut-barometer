"""Photostream, albums and photo detail."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ...planner import photo_page as flickr_url
from ..deps import session, store

router = APIRouter(prefix="/api", tags=["gallery"], dependencies=[Depends(session)])
Sort = Literal["newest", "oldest", "taken", "views", "faves", "comments", "title"]


@router.get("/photos")
def photos(q: str = Query("", max_length=100), sort: Sort = "newest", media: Literal["photo", "video"] | None = None,
           album: str | None = Query(None, max_length=40), tag: str = Query("", max_length=100),
           year: int | None = Query(None, ge=1800, le=3000), camera: str = Query("", max_length=200),
           limit: int = Query(60, ge=1, le=200), offset: int = Query(0, ge=0), db=Depends(store)):
    """The Photos page and every filtered view of it (album, tag, year, camera, search)."""
    return db.photo_page(album_id=album, q=q, sort=sort, media=media, limit=limit, offset=offset, tag=tag,
                         year=year, camera=camera)


@router.get("/photo-years")
def photo_years(db=Depends(store)):
    """Years taken, with counts, for the Photos page's year filter."""
    return db.years_taken()


@router.get("/photos/{photo_id}")
def photo(photo_id: str, db=Depends(store)):
    p = db.photo_detail(photo_id)
    if not p:
        raise HTTPException(404, "no such photo")
    return {**p, "url": flickr_url(db.get_meta("nsid", "me"), photo_id), "engagement": db.engagement(photo_id)}


@router.get("/albums")
def albums(db=Depends(store)):
    return db.album_list()


@router.get("/albums/{album_id}")
def album(album_id: str, sort: Sort = "newest", limit: int = Query(60, ge=1, le=200), offset: int = Query(0, ge=0),
          db=Depends(store)):
    a = db.album(album_id)
    if not a:
        raise HTTPException(404, "no such album")
    return {**a, **db.photo_page(album_id=album_id, sort=sort, limit=limit, offset=offset)}
