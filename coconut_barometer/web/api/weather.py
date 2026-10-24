"""The Weather pages: stations, the live view, history for charts, a wind rose, the radar/satellite map.
Public (read-only); starting a sync needs a login."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from collections import deque
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from ...errors import caught
from ...weather.imagery import Imagery
from ..deps import client_ip, runtime, session, store, viewer

router = APIRouter(prefix="/api/wx", tags=["weather"])
logger = logging.getLogger(__name__)


# ---- the Map tab's own errors (#65) ---------------------------------------------------------------------------
class MapError(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    stack: str = Field("", max_length=6000)
    source: Literal["error", "unhandledrejection"]
    name: str = Field("Error", min_length=1, max_length=100)


class MapErrorThrottle:
    """At most `per_client` reports a minute from one address and `global_limit` from all of them, remembering at
    most `max_clients` addresses: the endpoint takes reports from visitors too."""

    def __init__(self, per_client: int = 5, global_limit: int = 60, window: int = 60, max_clients: int = 4096):
        self.per_client, self.global_limit, self.window, self.max_clients = per_client, global_limit, window, max_clients
        self.lock = threading.Lock()
        self.clients: dict[str, deque[float]] = {}
        self.requests: deque[float] = deque()

    def allow(self, client: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        with self.lock:
            while self.requests and self.requests[0] <= cutoff:
                self.requests.popleft()
            for ip in [ip for ip, hits in self.clients.items() if not hits or hits[-1] <= cutoff]:
                del self.clients[ip]
            hits = self.clients.get(client)
            if hits is not None:
                while hits and hits[0] <= cutoff:
                    hits.popleft()
            if len(self.requests) >= self.global_limit or (hits is None and len(self.clients) >= self.max_clients):
                return False
            hits = self.clients.setdefault(client, deque())
            if len(hits) >= self.per_client:
                return False
            hits.append(now)
            self.requests.append(now)
            return True


_map_error_throttle = MapErrorThrottle()
# The same place in the code whatever the site's address or the deploy: no host, no /static/v/<build>/, no query,
# no line and column.
_FRAME_NOISE = [(re.compile(r"^[a-z][a-z0-9+.-]*://[^/]+"), ""), (re.compile(r"/static/v/[0-9a-f]+/"), "/static/"),
                (re.compile(r"[?#].*$"), ""), (re.compile(r":\d+(?::\d+)?$"), "")]


def _map_error_fingerprint(body: MapError) -> str:
    """The error's name and message and its innermost frame (Chrome's `at f (url:l:c)` or Firefox/Safari's
    `f@url:l:c`), so one bug is one issue across deploys."""
    callsite = "?"
    for line in body.stack.splitlines():
        frame = line.strip()
        if frame.startswith("at "):
            frame = frame[3:]
            function, _, place = frame.rpartition(" (") if frame.endswith(")") else ("", "", frame)
            place = place.rstrip(")")
        elif "@" in frame:
            function, _, place = frame.rpartition("@")
        else:
            continue
        for pattern, to in _FRAME_NOISE:
            place = pattern.sub(to, place)
        callsite = f"{function}@{place}"
        break
    return hashlib.sha1(f"{body.name}\n{body.message}\n{callsite}".encode()).hexdigest()[:12]


def _units(rt) -> str:
    return rt.cfg["display"]["units"]


@router.get("/stations")
def stations(rt=Depends(runtime), db=Depends(store)):
    """The Tempest stations known so far, with how much history each has, and the stream's state."""
    return {"configured": rt.weather.token() is not None, "units": _units(rt),
            "backfill_days": rt.cfg["weatherflow"]["backfill_days"],
            "stations": db.wx_stations(), "stream": rt.weather.hub.snapshot()["stream"]}


@router.post("/sync", dependencies=[Depends(session)])
def sync(rt=Depends(runtime)):
    """Queue a REST sync: refresh the stations, fetch missing history. Shows on the Status page."""
    return rt.review.wx_sync()


# The stations part of /live, per database: rebuilt when a new observation or event is stored (or after a
# minute, for anything else a sync changes). Every Live viewer polls every 3 s; observations come once a minute.
_live_cache: dict[str, tuple] = {}
LIVE_CACHE_SECONDS = 60


def _latest(rt, db) -> list[dict]:
    key, now = db.wx_freshness(), time.time()
    hit = _live_cache.get(rt.db_path)
    if hit and hit[0] == key and now - hit[1] < LIVE_CACHE_SECONDS:
        return hit[2]
    stations = [s for s in (db.wx_latest(d["device_id"]) for d in db.wx_devices()) if s]
    _live_cache[rt.db_path] = (key, now, stations)
    return stations


@router.get("/live")
def live(rt=Depends(runtime), db=Depends(store)):
    """Each station's newest observation plus the 3-second wind from the stream. Poll this every few seconds."""
    snap = rt.weather.hub.snapshot()
    return {"units": _units(rt), "now": int(time.time()), "stream": snap["stream"], "rapid": snap["rapid"],
            "events": snap["events"], "stations": _latest(rt, db)}


def _window(start, end, hours):
    end = end or int(time.time())
    start = start if start is not None else int(end - hours * 3600)
    if start >= end:
        raise HTTPException(422, "start must be before end")
    return start, end


@router.get("/history")
def history(device: list[int] = Query(...), start: int | None = None, end: int | None = None,
            hours: float = Query(24, gt=0, le=24 * 3650), db=Depends(store), rt=Depends(runtime)):
    """Charts: one series set per device, on shared timestamps. `start`/`end` (epoch seconds), or the last `hours`."""
    start, end = _window(start, end, hours)
    if not set(device) <= {d["device_id"] for d in db.wx_devices()}:
        raise HTTPException(404, "unknown device")
    return {"units": _units(rt), "start": start, "end": end, **db.wx_history(device, start, end)}


@router.get("/lightning")
def lightning(device: int, start: int | None = None, end: int | None = None, hours: float = Query(24, gt=0, le=24 * 3650),
              db=Depends(store)):
    """Lightning strike counts in 30-minute bins by distance (km), for the heatmap."""
    start, end = _window(start, end, hours)
    if device not in {d["device_id"] for d in db.wx_devices()}:
        raise HTTPException(404, "unknown device")
    return {"start": start, "end": end, **db.wx_lightning(device, start, end)}


@router.get("/days")
def days(device: int, start: int | None = None, end: int | None = None, hours: float = Query(24 * 7, gt=0, le=24 * 3650),
         db=Depends(store)):
    """Temperature by local day: the daily low/high/mean and the 24 hourly means, for the band and the heatmap."""
    start, end = _window(start, end, hours)
    if device not in {d["device_id"] for d in db.wx_devices()}:
        raise HTTPException(404, "unknown device")
    return {"start": start, "end": end, **db.wx_days(device, start, end)}


_records_cache: dict[tuple, tuple] = {}


@router.get("/records")
def records(device: int, rt=Depends(runtime), db=Depends(store)):
    """The station's records (all-time and per month), today's date in past years, and today's almanac.
    Rebuilt only when something new is stored (as /live)."""
    if device not in {d["device_id"] for d in db.wx_devices()}:
        raise HTTPException(404, "unknown device")
    key, now = db.wx_freshness(), time.time()
    hit = _records_cache.get((rt.db_path, device))
    if hit and hit[0] == key and now - hit[1] < LIVE_CACHE_SECONDS:
        return {"units": _units(rt), **hit[2]}
    out = db.wx_records(device, int(now))
    _records_cache[(rt.db_path, device)] = (key, now, out)
    return {"units": _units(rt), **out}


_imagery = Imagery()


def imagery() -> Imagery:
    return _imagery


@router.get("/map")
def map_layers(db=Depends(store), rt=Depends(runtime), im: Imagery = Depends(imagery)):
    """The Map tab: where the stations are, and the radar and satellite frames available now."""
    return {"units": _units(rt), "now": int(time.time()),
            "stations": [{k: s[k] for k in ("device_id", "name", "lat", "lon")} for s in db.wx_stations() if s["lat"] is not None],
            **im.frames()}


def _bbox(bbox: str | None) -> tuple | None:
    """`west,south,east,north` in degrees (the map's view, padded by the browser), or None for the whole US."""
    if not bbox:
        return None
    try:
        w, s, e, n = (float(x) for x in bbox.split(","))
    except ValueError:
        raise HTTPException(422, "bbox: west,south,east,north")
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise HTTPException(422, "bbox: west,south,east,north")
    return w, s, e, n


def _cells(cells: list[dict], at: float, bbox: tuple | None) -> list[dict]:
    """Storm cells (in `bbox`, give or take a degree), with where each is at `at` and 30 and 60 minutes on."""
    from ...weather.radar import where_at

    if bbox:
        w, s, e, n = bbox
        cells = [c for c in cells if w - 1 <= c["lon"] <= e + 1 and s - 1 <= c["lat"] <= n + 1]
    return [{**{k: c[k] for k in ("id", "lat", "lon", "toward", "kmh", "dbz", "posh", "tvs", "valid")},
             "path": [where_at(c, at + m * 60) for m in (0, 30, 60)]} for c in cells]


def _with_shapes(rest: dict, shapes: str) -> Response:
    """`rest` as JSON with the shapes (radar.view()'s ready-made text) spliced in, not decoded and encoded again."""
    return Response(json.dumps(rest, separators=(",", ":"))[:-1] + ',"shapes":' + shapes + "}", media_type="application/json")


@router.post("/map/errors")
def map_error(body: MapError, request: Request, me=Depends(viewer)):
    """A Weather map error in the browser: logged, and (from a signed-in browser) reported to GitHub like the
    server's own (errors.py). A visitor's is only logged: anyone can send one, and GitHub issues aren't theirs to open."""
    if not _map_error_throttle.allow(client_ip(request)):
        raise HTTPException(429, "too many browser errors; try again later")
    where = f"weather map {body.source}"
    if me is None:
        logger.warning("%s (a visitor's browser): %s: %s\n%s", where, body.name, body.message, body.stack[:2000])
        return {"reported": False}
    error = RuntimeError(f"{body.name}: {body.message}\n{body.stack}".rstrip())
    error._coconut_fingerprint = _map_error_fingerprint(body)
    caught(error, where)
    return {"reported": True}


@router.get("/radar")
def radar(bbox: str | None = Query(None, max_length=100), zoom: float | None = Query(None, ge=0, le=24), rt=Depends(runtime)):
    """The Map tab's smooth radar: the newest MRMS frame as shapes per dBZ level and up (weather/radar.py view():
    zoomed in, ~2 km and only what's in `bbox`; zoomed out, ~8 km over the US), colored in the browser from
    Settings → Display → Radar colors; and the storm cells, with where each will be in 30 and 60 minutes."""
    from ...weather.radar import view

    box, snap, now = _bbox(bbox), rt.radar.snapshot(), time.time()
    frame = snap["frame"]
    rest = {"t": frame and frame["t"], "colors": rt.cfg["theme"]["radar_colors"], "cells": _cells(snap["cells"], now, box),
            "cells_at": snap["cells_at"], "error": snap["error"], "units": _units(rt)}
    return _with_shapes(rest, view(frame, box, zoom) if frame else '{"level":null,"q":1,"bands":[]}')


@router.get("/radar/snapshot.png")
def radar_snapshot(request: Request, theme: Literal["dark", "light"] = "dark", scale: int = Query(1, ge=1, le=2),
                   t: int | None = None, db=Depends(store), rt=Depends(runtime), _=Depends(session)):
    """Home's Radar card (#77): the newest radar around the stations as a PNG (weather/snapshot.py), rendered once per
    MRMS frame and theme. `t` (the frame's time) only makes each frame its own address, for the browser's cache.
    No frame yet, or a stale one: the basemap saying "Radar unavailable", not an error."""
    stations = rt.radar.stations(db)
    tz = next((s["timezone"] for s in db.wx_stations() if s.get("timezone")), None)
    etag, png = rt.snapshots.get(stations, theme, scale, rt.cfg["theme"]["radar_colors"], rt.cfg["map"]["carto_key"], tz)
    headers = {"ETag": etag, "Cache-Control": "private, max-age=120"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(png, media_type="image/png", headers=headers)


@router.get("/radar/glance")
def radar_glance(db=Depends(store), rt=Depends(runtime), _=Depends(session)):
    """Home's Radar card's lines: the frame's time, the nearest rain to the first station (how far, which way), and
    how many storm cells are within Settings → Alerts → storm distance of any station."""
    from ...weather.radar import _km, nearest, where_at
    from ...weather.snapshot import STALE

    stations, now = rt.radar.stations(db), time.time()
    with rt.radar.lock:
        field, cells = rt.radar.field, list(rt.radar.cells)
    fresh = bool(field) and now - field["t"] <= STALE
    near = None
    if fresh and stations:
        s = stations[0]
        found = nearest(field, field["z"], s["lat"], s["lon"])
        near = found and {"km": round(found[0], 1), "toward": round(found[1]), "station": s["name"]}
    radius = rt.cfg["alerts"]["storm_km"] or 10
    storms = sum(1 for c in cells if any(math.hypot(*_km(s["lat"], s["lon"], *where_at(c, now))) <= radius for s in stations))
    return {"t": field and field["t"], "fresh": fresh, "nearest": near, "storms": storms, "storm_km": radius,
            "stations": [s["name"] for s in stations], "units": _units(rt)}


@router.get("/fronts")
def surface_fronts():
    """The Map tab's fronts and highs and lows: WPC's latest surface analysis (weather/fronts.py), every 3 hours."""
    from ...weather.fronts import fronts

    return fronts.latest()


@router.get("/hazards")
def watches_warnings(bbox: str | None = Query(None, max_length=100), zoom: float | None = Query(None, ge=0, le=24)):
    """The Map tab's watches, warnings and advisories in effect now (weather/hazards.py), most important first, with
    their areas as radar's shapes are (zoomed in, fine and only what's in `bbox`; zoomed out, coarse over the US)."""
    from ...weather.hazards import hazards

    shapes, state = hazards.view(_bbox(bbox), zoom)
    rest = {k: state[k] for k in ("checked_at", "error", "pending")}
    return _with_shapes(rest, shapes)


@router.get("/hazards/alert")
def hazard_alert(id: str = Query(..., min_length=1, max_length=200)):
    """An alert's headline, area and instruction for its popup, from the NWS API: only for an alert on the map."""
    from ...weather.hazards import hazards

    if not hazards.known(id):
        raise HTTPException(404, "no such alert in effect")
    try:
        return hazards.detail(id)
    except Exception as e:  # noqa: BLE001 - the NWS API being down: the popup shows what the map already has
        caught(e, "alert text")
        raise HTTPException(502, "the NWS couldn't be reached")


@router.get("/radar/frames")
def radar_frames(rt=Depends(runtime)):
    """Times of the radar frames (MRMS shapes with the storm tracks then) kept for the Map tab's loop."""
    return {"frames": [{"t": t} for t in rt.radar.history()]}


@router.get("/radar/frame")
def radar_frame(t: int, bbox: str | None = Query(None, max_length=100), zoom: float | None = Query(None, ge=0, le=24),
                rt=Depends(runtime)):
    """One kept loop frame, shaped like /radar (for the same view): its shapes, and the storm cells as they were
    then, with where each was heading from there."""
    from ...weather.radar import view

    box = _bbox(bbox)
    frame = rt.radar.stored(t)
    if not frame or "levels" not in frame:             # (or kept before the shapes had levels: gone within the hour)
        raise HTTPException(404, "no such frame")
    return _with_shapes({"t": frame["t"], "cells": _cells(frame["cells"], t, box)}, view(frame, box, zoom))


@router.get("/rose")
def rose(device: int, start: int | None = None, end: int | None = None, hours: float = Query(24 * 7, gt=0, le=24 * 3650),
         part: str | None = Query(None, pattern="^(night|morning|afternoon|evening)$"), db=Depends(store)):
    """Wind by direction and speed band; `part` keeps one quarter of the station's local day."""
    start, end = _window(start, end, hours)
    if device not in {d["device_id"] for d in db.wx_devices()}:
        raise HTTPException(404, "unknown device")
    return db.wx_wind_rose(device, start, end, part)
