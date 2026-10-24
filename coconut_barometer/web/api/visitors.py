"""The site's own visitors (#83, visitors.py): /api/seen, where every visitor's page sends its route changes, and
/api/analytics/visitors, the signed-in report on the Visitors page (#/visitors)."""
from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, Depends, Query, Request, Response

from ...visitors import OWNER_COOKIE, PREVIEWS
from ..deps import client_ip, runtime, session, store
from ..security import COOKIE

router = APIRouter(tags=["visitors"])
BODY_MAX = 2048
OWNER_YEARS = 5


def mark_owner(response: Response, rt) -> None:
    """Signing in marks this device as the owner's, so it isn't counted even once signed out. It carries nothing
    (anyone setting it only leaves themselves out)."""
    response.set_cookie(OWNER_COOKIE, "1", max_age=OWNER_YEARS * 365 * 86400, httponly=True, samesite="lax",
                        secure=rt.cfg["web"]["secure_cookies"], path="/")


def _left_out(request: Request, rt) -> str | None:
    """Why this request isn't counted, or None."""
    if request.cookies.get(COOKIE) or request.cookies.get(OWNER_COOKIE):
        return "owner"
    if rt.cfg["visitors"]["honor_gpc"] and "1" in (request.headers.get("sec-gpc"), request.headers.get("dnt")):
        return "gpc"
    origin, site = request.headers.get("origin"), request.headers.get("sec-fetch-site")
    if (origin and origin.split("://")[-1] != request.headers.get("host")) or (site and site != "same-origin"):
        return "cross-site"
    return None


@router.post("/api/seen", include_in_schema=False)
async def seen(request: Request, rt=Depends(runtime)):
    """One page view: {p: the #/ route, r: document.referrer (first view only), w: the window's width,
    u: {source, medium, campaign} from utm_* in the address}. Sent with sendBeacon, so always 204, whatever it was."""
    done = Response(status_code=204)
    visitors = rt.visitors
    if not visitors.enabled:
        return done
    why = _left_out(request, rt)
    if why:
        visitors.leave_out(why)
        return done
    raw = await request.body()
    try:
        body = json.loads(raw) if len(raw) <= BODY_MAX else None
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("p"), str):
        visitors.leave_out("malformed")
        return done
    utm = body.get("u") if isinstance(body.get("u"), dict) else {}
    visitors.hit("page", body["p"][:300], client_ip(request), request.headers.get("user-agent", ""),
                 referrer=body.get("r") if isinstance(body.get("r"), str) else "",
                 own_host=(request.headers.get("host") or "").split(":")[0], width=body.get("w"), utm=utm)
    return done


def count_preview(request: Request, rt, path: str) -> None:
    """A share page (/p/, /a/) fetched by a link previewer: someone pasted the link somewhere."""
    if PREVIEWS.search(request.headers.get("user-agent", "")):
        rt.visitors.hit("preview", path, client_ip(request), request.headers.get("user-agent", ""))


def _since(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=days - 1)).isoformat()


@router.get("/api/analytics/visitors", dependencies=[Depends(session)])
def report(days: int = Query(30, ge=1, le=3650), db=Depends(store), rt=Depends(runtime)):
    """Days are UTC, today included. Page views still held in memory are written first, so the newest are in."""
    rt.visitors.flush(db)
    since, before = _since(days), _since(2 * days)
    keep = rt.cfg["visitors"]["keep_days"]
    previous = db.visitors_totals(before) if 2 * days <= keep else None
    current = db.visitors_totals(since)
    if previous:       # a visitor's id lasts a day, so the two periods never share one: subtracting is exact
        previous = {k: previous[k] - current[k] for k in ("visitors", "views", "sessions")}
    top = lambda by, n=20, kind="page": db.visitors_top(since, by, n, kind)
    photos = db.visitors_items(since, "/photos/photo/:id")
    cards = db.cards([r["photo_id"] for r in photos])
    photos = [{**r, "sizes": (cards.get(r["photo_id"]) or {}).get("sizes", {})} for r in photos]
    return {"since": since, "keep_days": keep, "totals": current, "previous": previous,
            "series": db.visitors_series(since),
            "pages": top("route"), "referrers": top("ref_host"), "campaigns": top("utm"),
            "cities": top("city", 30), "regions": top("region"), "countries": top("country"),
            "devices": top("device", 5), "browsers": top("browser", 10), "systems": top("os", 10),
            "bots": top("browser", 10, "bot"),
            "photos": photos, "previews": db.visitors_previews(since),
            "recent": db.visitors_recent(), "collector": rt.visitors.status()}
