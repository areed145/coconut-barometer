"""The Queue page's log: every call out, stream message and finished job, the calls in flight or waiting
for a rate-limit slot, and each source's totals. Poll with `since` (the last `seq` seen) for what's new."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ... import activity
from ..deps import runtime, session

router = APIRouter(prefix="/api/activity", tags=["activity"], dependencies=[Depends(session)])


@router.get("")
def recent(since: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=2000), rt=Depends(runtime)):
    entries = activity.log.since(since, limit)
    return {"seq": entries[-1]["seq"] if entries else since, "entries": entries, **activity.log.snapshot(),
            "streams": {"wx-stream": rt.weather.hub.snapshot()["stream"] if rt.weather.token() else None,
                        "aprs": {**rt.aprs.hub.snapshot(), "callsigns": rt.aprs.callsigns()}}}
