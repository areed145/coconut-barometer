"""The review queue: photos sent to review, their proposed changes, and what you do with each:
apply, ignore or re-process. Plus the jobs behind them."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..deps import runtime, session

router = APIRouter(prefix="/api/review", tags=["review"], dependencies=[Depends(session)])
Sort = Literal["newest", "oldest", "views", "faves", "comments"]


class Choice(BaseModel):
    approve: bool
    value: Any = None      # edited title/description text, the list of tags kept, or {lat, lon, accuracy}


class Decision(BaseModel):
    choices: list[Choice]


@router.get("/summary")
def summary(rt=Depends(runtime)):
    return rt.review.summary()


@router.get("/queue")
def queue(sort: Sort = "newest", rt=Depends(runtime)):
    """Every photo in review."""
    return rt.review.queue(sort)


@router.get("/find")
def find(q: str = Query(..., max_length=300), rt=Depends(runtime)):
    pid = rt.review.find(q)
    if not pid:
        raise HTTPException(404, "not in your library")
    return {"photo_id": pid}


@router.get("/places")
def places(q: str = Query(..., min_length=2, max_length=200), rt=Depends(runtime)):
    """Find a place for a photo's location: OpenStreetMap's Nominatim, or the offline town list."""
    return rt.review.places(q)


@router.get("/items/{photo_id}")
def item(photo_id: str, rt=Depends(runtime)):
    it = rt.review.item(photo_id)
    if not it:
        raise HTTPException(404, "no such photo")
    return it


@router.post("/items/{photo_id}/send")
def send(photo_id: str, rt=Depends(runtime)):
    """Into review (from the gallery): processed now or when opened, by Settings → Photos → Review."""
    try:
        return rt.review.send(photo_id)
    except KeyError:
        raise HTTPException(404, "not in your library")


@router.post("/items/{photo_id}/decide")
def decide(photo_id: str, body: Decision, rt=Depends(runtime)):
    """Apply: the changes you approved (as edited). Out of review once they're on Flickr."""
    try:
        return rt.review.decide(photo_id, [c.model_dump(exclude_none=True) for c in body.choices])
    except KeyError:
        raise HTTPException(404, "not in review")


@router.post("/items/{photo_id}/ignore")
def ignore(photo_id: str, rt=Depends(runtime)):
    """Out of review, changing nothing."""
    try:
        return rt.review.ignore(photo_id)
    except KeyError:
        raise HTTPException(404, "not in review")


@router.post("/items/{photo_id}/process")
def process(photo_id: str, if_new: bool = False, rt=Depends(runtime)):
    """Re-process: a fresh look, staying in review. Its tags are proposed as one whole list."""
    try:
        return rt.review.process(photo_id, if_new=if_new)
    except KeyError:
        raise HTTPException(404, "not in your library")


@router.get("/worker")
def worker(rt=Depends(runtime)):
    """The Queue page: the job running now, what's waiting (Flickr and vision), recent results."""
    return rt.review.queue_view()


@router.get("/jobs")
def jobs(since: int = Query(0, ge=0), rt=Depends(runtime)):
    return rt.review.jobs_status(since)
