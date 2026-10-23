"""Settings → Alerts: turning notifications on and off for a device, and a test. Signed-in only.

GET /api/push: the key a browser subscribes with, and the devices turned on. POST /api/push/subscribe with the
browser's PushSubscription (and a name for the device), /api/push/unsubscribe with a device's id, and
/api/push/test to send one now (to one device, or all)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ... import push
from ..deps import session, store

router = APIRouter(prefix="/api/push", tags=["alerts"], dependencies=[Depends(session)])


class Subscribe(BaseModel):
    subscription: dict
    label: str = Field("", max_length=80)


class Device(BaseModel):
    id: str | None = Field(None, max_length=32)


def _public(d: dict) -> dict:
    return {k: d[k] for k in ("id", "label", "username", "added")}


@router.get("")
def devices(db=Depends(store)):
    return {"public_key": push.public_key(db), "devices": [_public(d) for d in push.devices(db)]}


@router.post("/subscribe")
def subscribe(body: Subscribe, request: Request, sess=Depends(session), db=Depends(store)):
    origin = f"{request.url.scheme}://{request.url.netloc}"
    try:
        return _public(push.subscribe(db, body.subscription, sess["username"], body.label or "A device", origin))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/unsubscribe")
def unsubscribe(body: Device, db=Depends(store)):
    return {"removed": push.unsubscribe(db, body.id or "")}


@router.post("/test")
def test(body: Device, db=Depends(store)):
    out = push.send(db, "Alerts are on", "This is how a lightning, gust, pressure or callsign alert will arrive.",
                    "/#/settings/alerts", tag="test", only=body.id)
    if not out["sent"] and not out["failed"]:
        raise HTTPException(404, "no device to send to")
    return out
