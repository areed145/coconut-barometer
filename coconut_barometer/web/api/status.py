"""Home's status panel: the health of everything the site depends on, a row each, worst first in `overall`.

    ok    working    warn  working, but something's behind or failed once    bad  not working
    off   not set up (left out of `overall`)

Visitors get the public services only (WeatherFlow, APRS-IS, radar and satellite), without error text; signed
in, every row, with the error and where it's fixed. Each row is read from what the app already tracks (the
streams' state, the activity log, the sync and backup records); nothing here calls out, except Flickr's
sign-in check, made at most every FLICKR_CHECK seconds. Cached for CACHE_SECONDS."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends

from ... import activity
from ..deps import runtime, store, viewer
from . import weather as wx_api

router = APIRouter(prefix="/api", tags=["status"])
CACHE_SECONDS, FLICKR_CHECK = 15, 600
STALE_WARN, STALE_BAD = 15 * 60, 60 * 60          # a station's newest observation, this old
LEVELS = ("ok", "off", "warn", "bad")             # worse to the right; "off" doesn't count
PUBLIC = ("weatherflow", "aprs", "imagery")
_cache: dict[tuple, tuple] = {}
_flickr_check: dict = {"at": 0.0, "status": None}   # the last sign-in check


def _ago(now: float, ts) -> str:
    if not ts:
        return "never"
    s = now - ts
    return "just now" if s < 90 else f"{round(s / 60)} min ago" if s < 5400 else f"{round(s / 3600)} h ago" if s < 129600 else f"{round(s / 86400)} days ago"


def row(id_, name, level, state, detail=None, href=None, at=None) -> dict:
    return {"id": id_, "name": name, "level": level, "state": state, "detail": detail, "href": href, "at": at}


def _weatherflow(rt, db, now) -> dict:
    if not rt.weather.token():
        return row("weatherflow", "WeatherFlow", "off", "no token", href="#/settings/connections")
    stream = rt.weather.hub.snapshot()["stream"]
    connected = stream.get("state") == "connected"
    newest = {d: t for d, t in db.db.execute("SELECT device_id, max(ts) FROM wx_obs GROUP BY 1").fetchall()}
    stations = [(s["name"], newest.get(s["device_id"])) for s in db.wx_stations()]
    oldest = min((t or 0 for _, t in stations), default=None)
    age = now - oldest if oldest else None
    level = "bad" if not stations or age is None or age > STALE_BAD else "warn" if age > STALE_WARN or not connected else "ok"
    behind = [f"{n}: last report {_ago(now, t)}" for n, t in stations if not t or now - t > STALE_WARN]
    state = ("live stream connected" if connected else f"live stream {stream.get('state') or 'not started'}") + (
        f" · {'; '.join(behind)}" if behind else f" · {len(stations)} station{'s' if len(stations) != 1 else ''} reporting")
    return row("weatherflow", "WeatherFlow", level, state, stream.get("error"), "#/settings/weather", max(newest.values(), default=None))


def _aprs(rt, now) -> dict:
    if not rt.aprs.callsigns() and not rt.aprs.igate():
        return row("aprs", "APRS-IS", "off", "nothing to follow", href="#/settings/aprs")
    s = rt.aprs.hub.snapshot()                         # the stream's state itself (WeatherFlow's nests it)
    ok = s.get("state") == "connected"
    heard = s.get("last_message")
    return row("aprs", "APRS-IS", "ok" if ok else "bad",
               (f"connected · last packet {_ago(now, heard)}" if heard else "connected · no packets yet") if ok
               else f"{s.get('state') or 'not started'}",
               s.get("error"), "#/settings/aprs", heard)


def _cwop(rt, now) -> dict:
    c = rt.aprs.cwop_status()
    if not c["reporting"]:
        return row("cwop", "CWOP reports", "off", "no station reports", href="#/settings/aprs")
    calls = ", ".join(r["callsign"] for r in c["reporting"])
    every = (c["every_minutes"] or 10) * 60
    if c["error"]:
        return row("cwop", "CWOP reports", "bad", f"{calls}: last round failed", c["error"], "#/settings/aprs", c["last_at"])
    late = c["last_at"] and now - c["last_at"] > 3 * every
    return row("cwop", "CWOP reports", "warn" if late else "ok",
               f"{calls} · {'last sent ' + _ago(now, c['last_at']) if c['last_at'] else 'first report due'}",
               None, "#/settings/aprs", c["last_at"])


def _flickr(rt, db, now) -> dict:
    if now - _flickr_check["at"] > FLICKR_CHECK or _flickr_check["status"] is None:
        _flickr_check.update(at=now, status=rt.flickr_status())    # a Flickr call: at most every FLICKR_CHECK
    f = _flickr_check["status"]
    if not f["keys_set"]:
        return row("flickr", "Flickr", "bad", "no API keys", href="#/settings/connections")
    if not f["connected"]:
        return row("flickr", "Flickr", "bad", "not signed in", href="#/settings/connections")
    last = int(db.get_meta("last_sync") or 0) or None
    result = json.loads(db.get_meta("last_sync_result") or "null") or {}
    every = rt.cfg["web"]["sync_every_minutes"] * 60
    if result.get("error"):
        return row("flickr", "Flickr", "warn", f"signed in · last sync failed", result["error"], "#/queue", last)
    late = every and last and now - last > 3 * every
    return row("flickr", "Flickr", "warn" if late else "ok", f"signed in · synced {_ago(now, last)}", None, "#/queue", last)


def _vision(rt, now) -> dict:
    v = rt.cfg["vision"]
    if v.get("provider") != "ollama" and not v.get("api_key"):
        return row("vision", "Vision model", "off", "no API key", href="#/settings/connections")
    last = activity.log.latest("vision")
    if last["error"]:
        return row("vision", "Vision model", "warn", f"{v.get('model')} · last call failed", last["error"], "#/queue", last["at"])
    return row("vision", "Vision model", "ok",
               f"{v.get('model')} · {'last used ' + _ago(now, last['at']) if last['at'] else 'not used since the app started'}",
               None, "#/queue", last["at"])


def _imagery(now) -> dict:
    cached = wx_api._imagery._cached                    # read what's there: never fetches for the panel
    if not cached:
        return row("imagery", "Radar & satellite", "ok", "checked when the weather map is opened")
    at, frames = cached
    down = [name for name, f in [("radar", frames["radar"]), *((f"satellite {k}", v) for k, v in frames["satellite"].items())]
            if f.get("error")]
    return row("imagery", "Radar & satellite", "warn" if down else "ok",
               f"{', '.join(down)} unavailable" if down else f"all frames available · checked {_ago(now, at)}",
               "; ".join(f"{n}: {frames['radar']['error'] if n == 'radar' else frames['satellite'][n.split()[1]]['error']}" for n in down) or None,
               "#/weather/map", int(at))


def _backups(rt, db, now) -> dict:
    every = rt.cfg["backup"]["every_hours"]
    if every <= 0:
        return row("backups", "Backups", "off", "turned off", href="#/settings/backup")
    last = int(db.get_meta("last_backup") or 0) or None
    error = db.get_meta("last_backup_error") or ""
    failed_at = int(error.split(":", 1)[0]) if error[:1].isdigit() else 0
    if failed_at and (not last or failed_at > last):
        return row("backups", "Backups", "bad", "last backup failed", error.split(":", 1)[1].strip(), "#/settings/backup", last)
    overdue = not last or now - last > 2 * every * 3600
    return row("backups", "Backups", "bad" if overdue else "ok", f"last backup {_ago(now, last)}", None, "#/settings/backup", last)


@router.get("/status")
def status(rt=Depends(runtime), db=Depends(store), me=Depends(viewer)):
    now = time.time()
    hit = _cache.get((rt.db_path, me is None))
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    rows = [_weatherflow(rt, db, now), _aprs(rt, now), _imagery(now)]
    if me is not None:
        rows += [_cwop(rt, now), _flickr(rt, db, now), _vision(rt, now), _backups(rt, db, now)]
    else:
        rows = [{**r, "detail": None, "href": None} for r in rows if r["id"] in PUBLIC and r["level"] != "off"]
    counted = [r["level"] for r in rows if r["level"] != "off"]
    out = {"overall": max(counted, key=LEVELS.index) if counted else "off", "checked_at": int(now), "rows": rows}
    _cache[(rt.db_path, me is None)] = (now, out)
    return out
