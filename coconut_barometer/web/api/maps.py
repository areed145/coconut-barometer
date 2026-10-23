"""The Map page: every geotagged photo at once (clustered in the browser), and the
cards for the photos in a cluster you open. Public: visitors see only public photos."""
from __future__ import annotations

import gzip
import json

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from ..deps import runtime, store, viewer
from .gallery import gradient

router = APIRouter(prefix="/api/map", tags=["map"])


class Ids(BaseModel):
    ids: list[str] = Field(max_length=500)


@router.get("/config")
def config(rt=Depends(runtime)):
    """What the browser needs to draw a map. The CARTO key is public by design (it's in every
    tile URL), unlike the API keys kept encrypted."""
    mcfg = rt.cfg["map"]
    return {"carto_key": mcfg["carto_key"] or "", "colors": gradient(rt.cfg),
            "expand_at": mcfg["expand_at"]}


@router.get("/points")
def points(request: Request, album: str | None = Query(None, max_length=40),
           year_from: int | None = Query(None, ge=1800, le=3000), year_to: int | None = Query(None, ge=1800, le=3000),
           rt=Depends(runtime), db=Depends(store), me=Depends(viewer)):
    """{points: [[id, lat, lon, server, secret], ...], years, colors, expand_at}. Gzipped here
    (it's the one big response, ~10 bytes a photo compressed); nothing secret is in it."""
    mcfg = rt.cfg["map"]
    body = json.dumps({"points": db.map_points(album, year_from, year_to, public_only=me is None),
                       "years": db.map_years(public_only=me is None),
                       "colors": gradient(rt.cfg), "expand_at": mcfg["expand_at"]},
                      separators=(",", ":")).encode()
    headers = {"Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", ""):
        body = gzip.compress(body, 6)
        headers["Content-Encoding"] = "gzip"
    return Response(body, media_type="application/json", headers=headers)


@router.post("/cards")
def cards(body: Ids, db=Depends(store), me=Depends(viewer)):
    """Cards (title, counts, image sizes) for the photos in a cluster, in the order asked."""
    found = db.cards(body.ids, public_only=me is None)
    return [found[i] for i in body.ids if i in found]
