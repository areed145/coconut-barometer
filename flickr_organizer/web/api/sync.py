"""Sync status and "sync now". The scheduler runs the same job every web.sync_every_minutes."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..deps import runtime, session

router = APIRouter(prefix="/api/sync", tags=["sync"], dependencies=[Depends(session)])


class Run(BaseModel):
    mode: Literal["full", "photo_list", "incremental"] | None = None   # None = whatever's due


@router.get("/status")
def status(rt=Depends(runtime)):
    return rt.sync_status()


@router.post("/run")
def run(body: Run, rt=Depends(runtime)):
    return rt.review.sync(body.mode)
