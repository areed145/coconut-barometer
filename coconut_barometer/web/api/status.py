"""The Status page: the health of everything the site depends on, the worker's jobs, and the log of every call.

/api/status, a row per connection, worst first in `overall` (Home's panel and the dot in the top bar read it too):

    ok    working    warn  working, but something's behind or failed once    bad  not working
    off   not set up (left out of `overall`)

Visitors get the public services only (WeatherFlow, APRS-IS, and the Weather map's satellite, radar, lightning,
fronts and watches and warnings), without error text; signed
in, every row, with the error and where it's fixed. Each row is read from what the app already tracks (the
streams' state, the activity log, the sync and backup records); nothing here calls out, except Flickr's
sign-in check, made at most every FLICKR_CHECK seconds. Cached for CACHE_SECONDS.

/api/status/jobs and /api/status/activity need a session: the job running now and the line behind it, and
every call out, stream message and finished job (poll with `since`, the last `seq` seen, for what's new)."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, Query

from ... import activity, local
from ...aprs.health import health
from ...errors import reporter
from ..deps import runtime, session, store, viewer
from . import weather as wx_api

router = APIRouter(prefix="/api/status", tags=["status"])
CACHE_SECONDS, FLICKR_CHECK = 15, 600
STALE_WARN, STALE_BAD = 15 * 60, 60 * 60          # a station's newest observation, this old
LEVELS = ("ok", "off", "warn", "bad")             # worse to the right; "off" doesn't count
PUBLIC = ("weatherflow", "aprs", "igate", "imagery", "radar", "lightning", "fronts", "hazards")
FEED_LATE = {"lightning": (10 * 60, 30 * 60), "fronts": (6 * 3600, 12 * 3600)}   # its newest data this much older than
                                                                                 # the check: warn, bad
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
    if local.on():
        return row("aprs", "APRS-IS", "off", local.STATE)
    if not rt.aprs.callsigns() and not rt.aprs.igate():
        return row("aprs", "APRS-IS", "off", "nothing to follow", href="#/settings/aprs")
    s = rt.aprs.hub.snapshot()                         # the stream's state itself (WeatherFlow's nests it)
    ok = s.get("state") == "connected"
    heard = s.get("last_message")
    return row("aprs", "APRS-IS", "ok" if ok else "bad",
               (f"connected · last packet {_ago(now, heard)}" if heard else "connected · no packets yet") if ok
               else f"{s.get('state') or 'not started'}",
               s.get("error"), "#/settings/aprs", heard)


def _igate(rt, db, now) -> dict:
    """The igate in Settings → APRS (#119): gating as usual, and still beaconing. Can't be told while APRS-IS is down."""
    gate = rt.aprs.igate()
    if local.on() or not gate:
        return row("igate", "Igate", "off", local.STATE if local.on() else "none set", href="#/settings/aprs")
    h = health(db, gate, int(now), rt.aprs.hub.snapshot(), rt.cfg["alerts"]["igate_quiet_minutes"] or 60)
    expected = f" (usually ~{h['expected_hour']:.0f})" if h["expected_hour"] is not None else ""
    state = f"{gate} · {h['last_hour']} gated in the last hour{expected} · last packet {_ago(now, h['last_ts'])}"
    if not h["connected"]:
        return row("igate", "Igate", "off", f"{gate} · can't tell until APRS-IS has been connected a while", href="#/aprs/igate")
    trouble = ("not gating: quiet when it usually isn't" if h["gating"] == "quiet" else
               "gating well under its usual for 3 hours" if h["gating"] == "degraded" else
               f"its beacon stopped {_ago(now, h['beacon_ts'])}" if h["beacon_stale"] else None)
    level = "bad" if h["gating"] == "quiet" else "warn" if trouble else "ok"
    return row("igate", "Igate", level, f"{state} · {trouble}" if trouble else state, None, "#/aprs/igate", h["last_ts"])


def _cwop(rt, now) -> dict:
    if local.on():
        return row("cwop", "CWOP reports", "off", local.STATE)
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
    if local.on():
        return row("flickr", "Flickr", "off", local.STATE)
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
        return row("flickr", "Flickr", "warn", f"signed in · last sync failed", result["error"], "#/status", last)
    late = every and last and now - last > 3 * every
    return row("flickr", "Flickr", "warn" if late else "ok", f"signed in · synced {_ago(now, last)}", None, "#/status", last)


def _vision(rt, now) -> dict:
    v = rt.cfg["vision"]
    if v.get("provider") != "ollama" and not v.get("api_key"):
        return row("vision", "Vision model", "off", "no API key", href="#/settings/connections")
    last = activity.log.latest("vision")
    if last["error"]:
        return row("vision", "Vision model", "warn", f"{v.get('model')} · last call failed", last["error"], "#/status", last["at"])
    return row("vision", "Vision model", "ok",
               f"{v.get('model')} · {'last used ' + _ago(now, last['at']) if last['at'] else 'not used since the app started'}",
               None, "#/status", last["at"])


def _imagery(now) -> dict:
    cached = wx_api._imagery._cached                    # read what's there: never fetches for the panel
    if not cached:
        return row("imagery", "Satellite", "ok", "checked when the weather map is opened")
    at, frames = cached
    down = {k: v["error"] for k, v in frames["satellite"].items() if v.get("error")}
    return row("imagery", "Satellite", "warn" if down else "ok",
               f"{', '.join(down)} unavailable" if down else f"all frames available · checked {_ago(now, at)}",
               "; ".join(f"{k}: {e}" for k, e in down.items()) or None, "#/weather/map", int(at))


def _feed(id_, name, feed, now, describe, newest=None) -> dict:
    """A Weather map layer fetched while the map asks (weather/feed.py): what it last got, never fetching for the panel.
    It's checked only while someone has the map open, so a long-ago check isn't trouble; data that was already old
    when it was checked (FEED_LATE), or a failed check, is."""
    with feed._lock:
        data, checked, error = feed.data, feed.checked_at, feed.error
    if checked is None:
        return row(id_, name, "ok", "checked when the weather map shows it", href="#/weather/map")
    t = newest(data) if newest and data else None
    warn, bad = FEED_LATE.get(id_, (None, None))
    late = t is not None and warn is not None and checked - t > warn
    level = "bad" if data is None or (error and late) or (t is not None and bad and checked - t > bad) \
        else "warn" if error or late else "ok"
    state = (describe(data) if data else "nothing yet") + f" · checked {_ago(now, checked)}"
    if error:
        state = "last check failed · " + state
    return row(id_, name, level, state, error, "#/weather/map", int(t or checked))


def _lightning(now) -> dict:
    from ...weather.glm import glm

    return _feed("lightning", "Lightning", glm, now, newest=lambda d: d["newest"],
                 describe=lambda d: f"GOES-East GLM, newest {_ago(glm.checked_at or now, d['newest'])} when checked · "
                                    f"{len(d['t']):,} flashes held" if d["newest"] else "no flashes held")


def _fronts(now) -> dict:
    from ...weather.fronts import fronts

    return _feed("fronts", "Fronts", fronts, now, newest=lambda d: d["valid"],
                 describe=lambda d: f"WPC analysis for {time.strftime('%H:%MZ', time.gmtime(d['valid']))}, "
                                    f"{len(d['fronts'])} fronts")


def _hazards(now) -> dict:
    from ...weather.hazards import hazards

    def describe(d):
        major = sum(not a["minor"] for a in d["alerts"])
        return f"{major} watch{'es' if major != 1 else ''} and warnings, {len(d['alerts']) - major} advisories and statements"
    return _feed("hazards", "Watches & warnings", hazards, now, describe=describe)


def _qc(rt, db, now) -> dict:
    """Station QC's sampling (#105): signed in only, like the stations' details."""
    if not rt.cfg["qc"]["enabled"]:
        return row("qc", "Station QC", "off", "turned off", href="#/settings/weather")
    if local.on():
        return row("qc", "Station QC", "off", local.STATE)
    newest = db.qc_sampled()["rtma"]["newest"]
    down = {k: e for k, e in rt.qc.errors.items() if e}
    late = newest is None or now - newest > 2 * 3600
    fill = rt.qc.backfill
    state = (f"RTMA sampled {_ago(now, newest)}" if newest else "no RTMA samples yet") + (
        f" · filling in the past, at {time.strftime('%b %d', time.gmtime(fill['to']))}" if fill.get("to") and not fill.get("done") else "")
    if down:
        state += f" · {', '.join(down)} failing"
    return row("qc", "Station QC", "warn" if down or (late and db.qc_stations()) else "ok", state,
               "; ".join(f"{k}: {e}" for k, e in down.items()) or None, "#/weather/qc", newest)


def _radar(rt, now) -> dict:
    snap = rt.radar.snapshot()
    if rt.radar.checked_at is None:
        return row("radar", "Radar", "off", "no station with a location", href="#/settings/weather")
    t = snap["frame"] and snap["frame"]["t"]
    level = "bad" if not t or now - t > 30 * 60 else "warn" if snap["error"] or now - t > 10 * 60 else "ok"
    state = f"MRMS frame {_ago(now, t)}" if t else "no frame yet"
    if snap["cells_at"]:
        state += f" · {len(snap['cells'])} nationwide storm cell{'s' if len(snap['cells']) != 1 else ''}"
    return row("radar", "Radar", level, state, snap["error"], "#/weather/map", t)


def _backups(rt, db, now) -> dict:
    every = rt.cfg["backup"]["every_hours"]
    if local.on():
        return row("backups", "Backups", "off", local.STATE)
    if every <= 0:
        return row("backups", "Backups", "off", "turned off", href="#/settings/backup")
    last = int(db.get_meta("last_backup") or 0) or None
    error = db.get_meta("last_backup_error") or ""
    failed_at = int(error.split(":", 1)[0]) if error[:1].isdigit() else 0
    if failed_at and (not last or failed_at > last):
        return row("backups", "Backups", "bad", "last backup failed", error.split(":", 1)[1].strip(), "#/settings/backup", last)
    overdue = not last or now - last > 2 * every * 3600
    return row("backups", "Backups", "bad" if overdue else "ok", f"last backup {_ago(now, last)}", None, "#/settings/backup", last)


def _visitors(rt, now) -> dict:
    v = rt.visitors.status()
    href = "#/settings/visitors"
    if not v["enabled"]:
        return row("visitors", "Visitors", "off", "not counted", href=href)
    if v["error"]:
        return row("visitors", "Visitors", "bad", "can't write page views", v["error"], href, v["flushed_at"])
    age = v["geoip_age"]
    if age is None:
        state = "downloading the city database" if v["geoip_updating"] else "no city database yet"
        return row("visitors", "Visitors", "warn", state, v["geoip_error"], href, v["flushed_at"])
    days = int(age // 86400)
    stale = days > 45
    return row("visitors", "Visitors", "warn" if stale else "ok",
               f"{v['written']:,} page views written since the app started · city database {days} days old",
               v["geoip_error"] if stale else None, href, v["flushed_at"])


def _error_reports(now) -> dict:
    r = reporter.status()
    if local.on():
        return row("errors", "Error reports", "off", local.STATE)
    if not r["enabled"]:
        return row("errors", "Error reports", "off", "no GitHub token", href="#/settings/connections")
    if r["error"]:
        return row("errors", "Error reports", "warn", f"{r['repo']} · last report failed", r["error"],
                   "#/settings/connections", r["last_at"])
    sent = f"{r['sent']} sent since the app started · last {_ago(now, r['last_at'])}" if r["sent"] else "none since the app started"
    return row("errors", "Error reports", "ok", f"{r['repo']} · {sent}", None, "#/settings/connections", r["last_at"])


@router.get("")
def status(rt=Depends(runtime), db=Depends(store), me=Depends(viewer)):
    now = time.time()
    hit = _cache.get((rt.db_path, me is None))
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    rows = [_weatherflow(rt, db, now), _aprs(rt, now), _igate(rt, db, now), _imagery(now), _radar(rt, now), _lightning(now), _fronts(now), _hazards(now)]
    if me is not None:
        rows += [_qc(rt, db, now), _cwop(rt, now), _flickr(rt, db, now), _vision(rt, now), _backups(rt, db, now),
                 _visitors(rt, now), _error_reports(now)]
    else:
        rows = [{**r, "detail": None, "href": None} for r in rows if r["id"] in PUBLIC and r["level"] != "off"]
    counted = [r["level"] for r in rows if r["level"] != "off"]
    out = {"overall": max(counted, key=LEVELS.index) if counted else "off", "checked_at": int(now), "rows": rows,
           "local": local.on()}
    _cache[(rt.db_path, me is None)] = (now, out)
    return out


@router.get("/jobs", dependencies=[Depends(session)])
def jobs(rt=Depends(runtime)):
    return rt.review.jobs_view()


@router.get("/activity", dependencies=[Depends(session)])
def recent(since: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=2000), rt=Depends(runtime)):
    entries = activity.log.since(since, limit)
    return {"seq": entries[-1]["seq"] if entries else since, "entries": entries, **activity.log.snapshot(),
            "streams": {"wx-stream": rt.weather.hub.snapshot()["stream"] if rt.weather.token() else None,
                        "aprs": {**rt.aprs.hub.snapshot(), "callsigns": rt.aprs.callsigns()}}}
