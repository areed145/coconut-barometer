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


class DuplicateDecision(BaseModel):
    action: Literal["keep", "delete"]


class Pick(BaseModel):
    photo_id: str
    index: int
    action: str            # the change's action, checked: the photo may have been re-planned since it was listed


class Bulk(BaseModel):
    verb: Literal["apply", "drop"]
    picks: list[Pick]


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


@router.post("/items/{photo_id}/duplicate")
def duplicate_decision(photo_id: str, body: DuplicateDecision, rt=Depends(runtime)):
    try:
        return rt.review.duplicate_decision(photo_id, body.action)
    except KeyError:
        raise HTTPException(404, "no duplicate decision is waiting")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/items/{photo_id}/process")
def process(photo_id: str, if_new: bool = False, rt=Depends(runtime)):
    """Re-process: a fresh look, staying in review. Its tags are proposed as one whole list."""
    try:
        return rt.review.process(photo_id, if_new=if_new)
    except KeyError:
        raise HTTPException(404, "not in your library")


@router.get("/bulk")
def bulk(kind: Literal["location", "tags", "title", "description", "album", "group"] = "location",
         value: str = Query("", max_length=300), rt=Depends(runtime)):
    """Bulk review: one kind of proposed change across every ready photo (value: a location's confidence,
    an album's or group's name), and how many of each kind are waiting."""
    return rt.review.bulk(kind, value)


@router.post("/bulk")
def bulk_decide(body: Bulk, rt=Depends(runtime)):
    """Apply (as proposed) or drop the picked changes; each photo's others stay in review."""
    return rt.review.bulk_decide(body.verb, [p.model_dump() for p in body.picks])


@router.post("/replan")
def replan(rt=Depends(runtime)):
    """Re-plan every ready photo with the current settings, from what's already known: no vision or Flickr calls."""
    return rt.review.replan()


@router.get("/applied")
def applied(limit: int = Query(50, ge=1, le=200), rt=Depends(runtime)):
    """The latest applies, newest first, and whether each can be undone."""
    return rt.review.applied(limit)


@router.post("/items/{photo_id}/undo")
def undo(photo_id: str, rt=Depends(runtime)):
    """Undo the photo's latest apply: what it replaced comes back, where Flickr still has what it wrote."""
    try:
        return rt.review.undo(photo_id)
    except KeyError:
        raise HTTPException(404, "nothing to undo")


@router.get("/jobs")
def jobs(since: int = Query(0, ge=0), rt=Depends(runtime)):
    return rt.review.jobs_status(since)
