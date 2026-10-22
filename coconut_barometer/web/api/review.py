"""The review queue: proposed changes per photo, your decisions, processing, jobs."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ...review import STATUSES
from ..deps import runtime, session

router = APIRouter(prefix="/api/review", tags=["review"], dependencies=[Depends(session)])
Status = Literal[STATUSES]  # type: ignore[valid-type]
Sort = Literal["newest", "oldest", "views", "faves", "comments"]


class Choice(BaseModel):
    approve: bool
    value: Any = None      # edited title/description text, or the list of tags kept


class Decision(BaseModel):
    choices: list[Choice]


@router.get("/summary")
def summary(rt=Depends(runtime)):
    return rt.review.summary()


@router.get("/queue")
def queue(status: Status = "pending", sort: Sort = "newest", rt=Depends(runtime)):
    return rt.review.queue(status, sort)


@router.get("/find")
def find(q: str = Query(..., max_length=300), rt=Depends(runtime)):
    pid = rt.review.find(q)
    if not pid:
        raise HTTPException(404, "not in your library")
    return {"photo_id": pid}


@router.get("/items/{photo_id}")
def item(photo_id: str, rt=Depends(runtime)):
    it = rt.review.item(photo_id)
    if not it:
        raise HTTPException(404, "no such photo")
    return it


@router.post("/items/{photo_id}/decide")
def decide(photo_id: str, body: Decision, rt=Depends(runtime)):
    try:
        return rt.review.decide(photo_id, [c.model_dump(exclude_none=True) for c in body.choices])
    except KeyError:
        raise HTTPException(404, "not in the queue")


@router.post("/items/{photo_id}/process")
def process(photo_id: str, if_new: bool = False, tags: Literal["add", "replace"] = "replace", rt=Depends(runtime)):
    """tags: "add" to the photo's tags, or "replace" them (shown for approval as one change)."""
    try:
        return rt.review.process(photo_id, if_new=if_new, tags=tags)
    except KeyError:
        raise HTTPException(404, "not in your library")


@router.get("/worker")
def worker(rt=Depends(runtime)):
    """The Queue page: the job running now, what's waiting (Flickr and vision), recent results."""
    return rt.review.queue_view()


@router.get("/jobs")
def jobs(since: int = Query(0, ge=0), rt=Depends(runtime)):
    return rt.review.jobs_status(since)
