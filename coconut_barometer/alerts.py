"""Alerts (Settings → Alerts): what's worth a notification, checked every CHECK_EVERY seconds from what the
streams have already stored, and sent with push.py to the devices turned on.

    lightning   a strike within alerts.lightning_km of a station (the live stream's strike events, and the
                strike count in observations, for when the stream missed one)
    gust        a gust over alerts.gust_kmh
    pressure    sea-level pressure down alerts.pressure_drop_hpa or more over 3 hours (the newest observation
                against the one 3 hours before it, as the Live card shows)
    callsign    a callsign from Settings → APRS heard after alerts.callsign_quiet_hours without a packet
    place       a tracked callsign arriving at or leaving a place (Settings → APRS → Places; aprs/places.py), per
                the rules in alerts.places, outside their quiet hours
    igate       the igate quiet or gating far less than usual, or its beacon stopped (aprs/health.py), and back
    record      the igate heard a station direct farther than any in alerts.igate_record_days (the long-distance log)
    altitude    a packet the igate gated from alerts.igate_altitude_km up or higher (a balloon, a flight), per callsign
    storm       a storm cell the NWS radars track (weather/radar.py) of at least alerts.storm_dbz whose path comes
                within alerts.storm_km of a station in the next alerts.storm_minutes
    rain        rain on radar closing in on a station, due within alerts.rain_minutes

The radar alerts read the radar's latest state; the others look only at what arrived since the last check (on startup, the last CHECK_EVERY), so a restart doesn't
replay old storms. The same alert for the same station or callsign is sent at most every
alerts.cooldown_minutes; when each was last sent is kept in the meta table, so a restart doesn't reset it."""
from __future__ import annotations

import json
import math
import time
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import push
from .aprs.health import DEGRADED_HOURS, health
from .aprs.igate import home
from .aprs import places as pl
from .weather import radar as rd

CHECK_EVERY = 60
STALE = 15 * 60          # pressure: only from an observation this fresh
SENT_META = "alerts_sent"
STORMS_META, RADAR_META, NOTED_META = "alerts_storms", "alerts_radar", "alerts_noted"
PLACES_META = "alerts_places"   # per callsign: the last position read, and in or out of each place
IGATE_META = "alerts_igate"     # which igate troubles have been alerted and not yet seen to clear
RAIN_FIT = 0.7           # how well the rain's distance must fit a steady approach (R²) to alert


def _history_url(device: int, chart: str, at: int) -> str:
    day = datetime.fromtimestamp(at, timezone.utc).date()
    start, end = (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()
    return f"/#/weather/history?from={start}&to={end}&station={device}&chart={chart}"


def _dist(km: float, units: str) -> str:
    return f"{km * 0.621371:.0f} mi" if units == "imperial" else f"{km:.0f} km"


def _speed(ms: float, units: str) -> str:
    return f"{ms * 2.23694:.0f} mph" if units == "imperial" else f"{ms * 3.6:.0f} km/h"


def _pressure(hpa: float, units: str, digits: int = 1) -> str:
    return f"{hpa * 0.02953:.{digits + 1}f} inHg" if units == "imperial" else f"{hpa:.{digits}f} hPa"


def _hours(s: float) -> str:
    return f"{s / 86400:.0f} days" if s >= 2 * 86400 else f"{s / 3600:.0f} hours"


def _span(s: float) -> str:
    return f"{round(s / 60)} min" if s < 5400 else _hours(s)


COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _minutes(s: float) -> str:
    return "now" if s < 90 else f"~{round(s / 60)} min"


class Alerts:
    def __init__(self, cfg: dict, callsigns=lambda: [], radar=None, igate=lambda: "", stream=lambda: {}):
        self.cfg, self.callsigns, self.radar, self.igate, self.stream = cfg, callsigns, radar, igate, stream
        self.since: int | None = None

    def check(self, store, now: float | None = None) -> list[dict]:
        """What's happened since the last check, as alerts ({key, title, body, url}), before the cool-down."""
        now = int(now or time.time())
        since, self.since = self.since or now - CHECK_EVERY, now
        a, units = self.cfg["alerts"], self.cfg["display"]["units"]
        out = []
        stations = store.db.execute(
            "SELECT d.device_id, s.name FROM wx_devices d JOIN wx_stations s ON s.station_id = d.station_id "
            "WHERE d.device_type = 'ST' ORDER BY s.name").fetchall()
        for dev, name in stations:
            if a["lightning_km"]:
                n, nearest = store.db.execute(
                    "SELECT COUNT(*), MIN(d) FROM (SELECT value AS d FROM wx_events WHERE device_id=? AND kind='strike' "
                    "AND ts > ? AND ts <= ? UNION ALL SELECT strike_dist FROM wx_obs WHERE device_id=? AND ts > ? AND "
                    "ts <= ? AND strike_count > 0 AND strike_dist > 0) WHERE d <= ?",
                    (dev, since, now, dev, since, now, a["lightning_km"])).fetchone()
                if n:
                    out.append({"key": f"lightning:{dev}", "url": _history_url(dev, "lightning", now),
                                "title": f"Lightning {_dist(nearest, units)} from {name}",
                                "body": f"Strikes within {_dist(a['lightning_km'], units)} in the last few minutes.",
                                "evidence": {"device_id": dev, "matching_records": n, "nearest_km": nearest,
                                             "threshold_km": a["lightning_km"], "window_start": since, "window_end": now}})
            if a["gust_kmh"]:
                gust_row = store.db.execute("SELECT wind_gust, ts FROM wx_obs WHERE device_id=? AND ts > ? AND ts <= ? "
                                            "ORDER BY wind_gust DESC, ts DESC LIMIT 1", (dev, since, now)).fetchone()
                gust, observed_at = gust_row if gust_row else (None, None)
                if gust is not None and gust * 3.6 >= a["gust_kmh"]:
                    out.append({"key": f"gust:{dev}", "url": _history_url(dev, "wind", now),
                                "title": f"Gust of {_speed(gust, units)} at {name}",
                                "body": f"Over the {_speed(a['gust_kmh'] / 3.6, units)} you set.",
                                "evidence": {"device_id": dev, "gust_ms": gust, "observed_at": observed_at,
                                             "threshold_kmh": a["gust_kmh"],
                                             "window_start": since, "window_end": now}})
            if a["pressure_drop_hpa"]:
                o = (store.wx_latest(dev) or {}).get("obs")
                change = o and o.get("pressure_change_3h")
                if change is not None and o["ts"] > now - STALE and change <= -a["pressure_drop_hpa"]:
                    out.append({"key": f"pressure:{dev}", "url": _history_url(dev, "pressure", now),
                                "title": f"Pressure falling at {name}",
                                "body": f"Down {_pressure(-change, units)} in 3 hours, now {_pressure(o['pressure_sl'], units)}.",
                                "evidence": {"device_id": dev, "observed_at": o["ts"], "pressure_hpa": o["pressure_sl"],
                                             "change_3h_hpa": change, "threshold_hpa": a["pressure_drop_hpa"]}})
        if self.radar is not None:
            out += self._radar(store, now, units)
        if a["callsign_quiet_hours"]:
            for call in self.callsigns():
                first = store.db.execute("SELECT MIN(ts) FROM aprs_positions WHERE callsign=? AND ts > ? AND ts <= ?",
                                         (call, since, now)).fetchone()[0]
                before = first and store.db.execute("SELECT MAX(ts) FROM aprs_positions WHERE callsign=? AND ts < ?",
                                                    (call, first)).fetchone()[0]
                if before and first - before >= a["callsign_quiet_hours"] * 3600:
                    out.append({"key": f"callsign:{call}", "url": "/#/aprs", "title": f"{call} heard",
                                "body": f"Its first packet in {_hours(first - before)}.",
                                "evidence": {"callsign": call, "first_packet_at": first, "previous_packet_at": before,
                                             "quiet_seconds": first - before}})
        if a["places"]:
            out += self._places(store, now, units)
        gate = self.igate()
        if a["igate_quiet_minutes"] and gate:
            out += self._igate(store, gate, now, units)
        if a["igate_record_days"] and gate:
            out += self._record(store, gate, since, now, units)
        if a["igate_altitude_km"] and gate:
            for h in store.igate_high(gate, since, now, a["igate_altitude_km"] * 1000):
                up = f"{h['altitude'] * 3.28084:,.0f} ft" if units == "imperial" else f"{h['altitude'] / 1000:.1f} km"
                out.append({"key": f"altitude:{h['callsign']}", "url": "/#/aprs/igate",
                            "title": f"{h['callsign']} heard at {up}",
                            "body": f"{gate} gated it{'' if h['direct'] else ' through a digipeater'}: a balloon or a flight.",
                            "evidence": {"callsign": h["callsign"], "igate": gate, "altitude_m": h["altitude"], "observed_at": h["ts"],
                                         "latitude": h["lat"], "longitude": h["lon"], "direct": h["direct"],
                                         "threshold_km": a["igate_altitude_km"], "window_start": since, "window_end": now}})
        return out

    def _places(self, store, now: int, units: str) -> list[dict]:
        """Arrive and leave alerts (#121). Each tracked callsign with a rule has its positions since the last check
        read in order through aprs/places.step for each place its rules name; a change waiting on a second position
        holds once it has stood alerts.place_dwell_minutes. Where each callsign is is kept (PLACES_META) so a
        restart picks up where it was, and the first position ever only sets it."""
        a = self.cfg["alerts"]
        where = {p["name"].lower(): p for p in pl.places(self.cfg)}
        tracked = set(self.callsigns())
        rules = [r for r in a["places"] if str(r["callsign"]).strip().upper() in tracked and str(r["place"]).strip().lower() in where]
        if not rules:
            return []
        zone = next((s["timezone"] for s in store.wx_stations() if s.get("timezone")), None) or "UTC"
        saved, out = json.loads(store.get_meta(PLACES_META) or "{}"), []
        for call in sorted({str(r["callsign"]).strip().upper() for r in rules}):
            mine = [r for r in rules if str(r["callsign"]).strip().upper() == call]
            names = sorted({str(r["place"]).strip().lower() for r in mine})
            st = saved.get(call) or {"ts": None, "places": {}}
            rows = store.db.execute(
                "SELECT ts, lat, lon FROM aprs_positions WHERE callsign=? AND ts <= ? ORDER BY ts DESC LIMIT 1" if st["ts"] is None
                else "SELECT ts, lat, lon FROM aprs_positions WHERE callsign=? AND ts > ? AND ts <= ? ORDER BY ts",
                (call, now) if st["ts"] is None else (call, st["ts"], now)).fetchall()
            events = []
            for ts, lat, lon in rows:
                for name in names:
                    state, ev = pl.step(st["places"].get(name), pl.verdict(where[name], lat, lon), ts, [lat, lon])
                    if ev:
                        events.append((name, ev, st["places"][name]["pending"]))
                    st["places"][name] = state
                st["ts"] = ts
            for name in names:
                s = st["places"].get(name)
                if s and s.get("pending") and now - s["pending"]["first"] >= a["place_dwell_minutes"] * 60:
                    p = s["pending"]
                    st["places"][name], ev = pl.settle(s)
                    events.append((name, ev, p))
            saved[call] = st
            for name, ev, p in events:
                place, (lat, lon) = where[name], p["at"]
                local = datetime.fromtimestamp(p["first"], ZoneInfo(zone))
                for r in mine:
                    if str(r["place"]).strip().lower() != name or r["on"] not in (ev, "both"):
                        continue
                    if pl.is_quiet(r.get("quiet") or "", local.hour * 60 + local.minute):
                        continue
                    d = pl.distance_m(place, lat, lon)
                    far = f"{d:.0f} m" if units != "imperial" else f"{d * 3.28084:.0f} ft"
                    out.append({"key": f"place:{call}:{name}:{ev}", "url": "/#/aprs",
                                "title": f"{call} {'arrived at' if ev == 'arrive' else 'left'} {place['name']}",
                                "body": f"At {local.strftime('%-I:%M %p').lower()}, {far} from its center.",
                                "evidence": {"callsign": call, "place": place["name"], "event": ev, "observed_at": p["first"],
                                             "latitude": lat, "longitude": lon, "distance_m": round(d), "radius_m": place["radius_m"]}})
                    break
        store.set_meta(PLACES_META, json.dumps(saved))
        return out

    def _igate(self, store, gate: str, now: int, units: str) -> list[dict]:
        """An alert when the igate stops gating or beaconing, and one when it's back. What's been alerted is kept
        (IGATE_META) until it clears, so each trouble is told once. Nothing while APRS-IS itself is down."""
        h = health(store, gate, now, self.stream(), self.cfg["alerts"]["igate_quiet_minutes"])
        if not h["connected"]:
            return []
        state, out = json.loads(store.get_meta(IGATE_META) or "{}"), []
        evidence = {"igate": gate, "last_packet_at": h["last_ts"], "packets_last_hour": h["last_hour"],
                    "expected_per_hour": h["rate"], "beacon_at": h["beacon_ts"]}
        last = f"its last packet {_span(now - h['last_ts'])} ago" if h["last_ts"] else "nothing gated yet"
        for trouble, bad, title, body in (
                ("gating", h["gating"], f"{gate} has gone quiet" if h["gating"] == "quiet" else f"{gate} is gating less than usual",
                 f"APRS-IS is fine, but {last}; it usually gates about {h['rate']:.0f} an hour now." if h["gating"] == "quiet"
                 else f"{h['last_hour']} packets in the last hour, under its usual for {DEGRADED_HOURS} hours running."),
                ("beacon", h["beacon_stale"], f"{gate}'s beacon has stopped",
                 f"Its own position beacon was last heard {_span(now - (h['beacon_ts'] or now))} ago." if h["beacon_stale"] else "")):
            if bad and trouble not in state:
                state[trouble] = now
                out.append({"key": f"igate:{trouble}", "url": "/#/aprs/igate", "title": title, "body": body,
                            "evidence": {**evidence, "trouble": bad if isinstance(bad, str) else trouble}})
            elif not bad and trouble in state:
                since = state.pop(trouble)
                out.append({"key": f"igate:{trouble}-back", "url": "/#/aprs/igate",
                            "title": f"{gate} is gating again" if trouble == "gating" else f"{gate}'s beacon is back",
                            "body": f"{_span(now - since)} after the alert.", "evidence": evidence})
        store.set_meta(IGATE_META, json.dumps(state))
        return out

    def _record(self, store, gate: str, since: int, now: int, units: str) -> list[dict]:
        """A new farthest believable direct station in alerts.igate_record_days, heard since the last check. Only
        against something already heard in that time, so the first packets ever aren't each a record."""
        at = home(self.cfg, store, gate)
        days = self.cfg["alerts"]["igate_record_days"]
        if not at:
            return []
        at = (at["lat"], at["lon"])
        new = store.igate_long(at, gate, since + 1, now, 1)["rows"]
        before = new and store.igate_long(at, gate, now - days * 86400, since, 1)["rows"]
        if not new or not before or new[0]["km"] <= before[0]["km"]:
            return []
        x, b = new[0], before[0]
        when = "ever" if days >= 3650 else f"in {days} days"
        return [{"key": "record", "url": "/#/aprs/igate",
                 "title": f"Farthest {when}: {x['callsign']} at {_dist(x['km'], units)}",
                 "body": f"Heard direct by {gate}, {COMPASS[round(x['bearing'] / 45) % 8]}. The farthest before was "
                         f"{b['callsign']} at {_dist(b['km'], units)}.",
                 "evidence": {"callsign": x["callsign"], "igate": gate, "distance_km": round(x["km"], 1),
                              "bearing_degrees": round(x["bearing"]), "observed_at": x["ts"], "path": x["path"],
                              "previous": b["callsign"], "previous_km": round(b["km"], 1), "previous_at": b["ts"],
                              "days": days, "window_start": since, "window_end": now}}]

    def _radar(self, store, now: int, units: str) -> list[dict]:
        """Storm and rain alerts for each station, with the guards against tracking errors (#71). A candidate a
        guard stops is still returned, with `why`, so run_once can note it in the alert log."""
        a, out = self.cfg["alerts"], []
        cells, fastest = self.radar.snapshot()["cells"], a["storm_max_kmh"]
        reach = a["storm_km"] + a["storm_minutes"] / 60 * fastest      # farther than this, nothing believable arrives
        for s in self.radar.stations(store):
            name, dev = s["name"], s["device_id"]
            if a["storm_km"]:
                due = []
                for c in cells:
                    if (c["dbz"] or 0) < a["storm_dbz"]:
                        continue
                    eta = rd.arrival(c, s["lat"], s["lon"], now, a["storm_km"], a["storm_minutes"] * 60)
                    if eta is None:
                        continue
                    lat, lon = rd.where_at(c, now)
                    east, north = rd._km(s["lat"], s["lon"], lat, lon)
                    km = math.hypot(east, north)
                    why = (f"moving {c['kmh']:.0f} km/h, faster than {fastest} km/h: a tracking error" if c["kmh"] > fastest and eta > 0
                           else f"{km:.0f} km away, beyond {reach:.0f} km" if km > reach
                           else "seen on one scan only, or its motion jumped" if c.get("steady", 1) < rd.STEADY_SCANS and eta > 0
                           else None)
                    due.append((why is not None, eta, c, km, math.degrees(math.atan2(east, north)) % 360, why))
                if due:
                    _, eta, c, km, bearing, why = min(due, key=lambda x: (x[0], x[1]))   # the soonest believable one
                    extra = [f"hail likely ({c['posh']}% severe)" if c["posh"] >= 30 else "",
                             "a tornado vortex signature" if c["tvs"] else ""]
                    moving = f"moving {COMPASS[round(c['toward'] / 45) % 8]} at {_speed(c['kmh'] / 3.6, units)}" if c["kmh"] else "barely moving"
                    where = f"{_dist(km, units)} {COMPASS[round(bearing / 45) % 8]}" if km >= 1 else "overhead"
                    out.append({"key": f"storm:{dev}:{c.get('track', c['id'])}", "kind": "storm", "station": dev, "why": why,
                                "url": "/#/weather/map",
                                "title": f"Storm over {name}" if eta == 0 else f"Storm {_minutes(eta)} from {name}",
                                "body": "; ".join(filter(None, [f"{c['dbz']} dBZ, {where}, {moving}", *extra])) + ".",
                                "evidence": {"device_id": dev, "cell_id": c.get("id"), "track": c.get("track"), "dbz": c["dbz"],
                                             "hail_probability": c["posh"], "tornado_vortex_signature": c["tvs"],
                                             "latitude": c["lat"], "longitude": c["lon"], "distance_km": round(km, 1),
                                             "speed_kmh": c["kmh"], "direction_degrees": c["toward"],
                                             "observed_kmh": c.get("obs_kmh"), "observed_direction": c.get("obs_toward"),
                                             "steady_scans": c.get("steady"), "radar_observed_at": c["valid"], "eta_seconds": eta,
                                             "radius_km": a["storm_km"]}})
            if a["rain_minutes"]:
                r = self.radar.closing(dev, now)
                if r and r["km"] > rd.HERE_KM and r["kmh"] >= 5 and now - r["t"] < 15 * 60:
                    eta = (r["km"] - rd.HERE_KM) / r["kmh"] * 3600
                    if eta <= a["rain_minutes"] * 60:
                        why = (f"a new echo nearer (a step of {r['step_kmh']:.0f} km/h), not an approach" if r["step_kmh"] > fastest
                               else f"closing at {r['kmh']:.0f} km/h, faster than {fastest} km/h" if r["kmh"] > fastest
                               else f"not a steady approach (fit {r['r2']:.2f})" if r["r2"] < RAIN_FIT
                               else None)
                        out.append({"key": f"rain:{dev}", "kind": "rain", "station": dev, "why": why, "url": "/#/weather/map",
                                    "title": f"Rain {_minutes(eta)} from {name}",
                                    "body": f"{_dist(r['km'], units)} away, closing at {_speed(r['kmh'] / 3.6, units)}.",
                                    "evidence": {"device_id": dev, "distance_km": r["km"], "speed_kmh": r["kmh"], "fit": round(r["r2"], 3),
                                                 "fastest_step_kmh": round(r["step_kmh"], 1), "radar_observed_at": r["t"], "eta_seconds": eta}})
        return out

    def _escalates(self, x: dict, last: dict, now: int) -> bool:
        """A storm alert within the cool-down of the last one for its station: only if this is worse news."""
        e, then = x["evidence"], last
        left = max(0, (then.get("eta") or 0) - (now - then["t"]))      # what the last alert's ETA has come down to
        return (e["dbz"] >= (then.get("dbz") or 0) + 5 or (e["hail_probability"] >= 30 > (then.get("posh") or 0))
                or (e["tornado_vortex_signature"] and not then.get("tvs"))
                or (e.get("track") == then.get("track") and left > 0 and e["eta_seconds"] < left / 2))

    def run_once(self, store, now: float | None = None) -> list[dict]:
        """Check, and send what's due past its cool-down. Returns what was sent. Nothing is checked (or sent)
        while no device is turned on. Radar alerts also go through the guards (#71): a storm within the cool-down
        of its station's last one only if it's worse news, at most alerts.radar_per_hour a station; each one held
        back is noted in the alert log (once per reason per cool-down)."""
        if not push.devices(store):
            self.since = None
            return []
        now = int(now or time.time())
        a = self.cfg["alerts"]
        cooldown = a["cooldown_minutes"] * 60
        sent = json.loads(store.get_meta(SENT_META) or "{}")
        storms = json.loads(store.get_meta(STORMS_META) or "{}")      # station -> its last storm alert
        radar_sent = json.loads(store.get_meta(RADAR_META) or "{}")   # station -> when its radar alerts went
        noted = json.loads(store.get_meta(NOTED_META) or "{}")        # "key|why" -> when it was noted
        due = []
        for x in self.check(store, now):
            # The notification opens the alert's own page (#73): its evidence, and the chart or map from then. The
            # full chart or map it used to open is linked from there.
            x["evidence"] = {**x.get("evidence", {}), "link": x["url"]}
            x["url"] = f"/#/alerts/{now}/{quote(x['key'], safe='')}"
            why, dev = x.get("why"), str(x.get("station", ""))
            if not why and x.get("kind") == "storm":
                last = storms.get(dev)
                if last and now - last["t"] < cooldown and not self._escalates(x, last, now):
                    why = "not worse than this station's storm alert in the cool-down"
            elif not why and now - sent.get(x["key"], 0) < cooldown:
                if x.get("kind") != "rain":
                    continue                                          # a plain cool-down: nothing to note
                why = "cool-down"
            if not why and x.get("kind"):
                recent = [t for t in radar_sent.get(dev, []) if now - t < 3600]
                if len(recent) >= a["radar_per_hour"]:
                    why = f"{len(recent)} radar alerts for this station in the last hour already"
            if why:
                if x.get("kind") and now - noted.get(f"{x['key']}|{why}", 0) >= cooldown:
                    noted[f"{x['key']}|{why}"] = now
                    store.alert_log_add(now, x, {"id": "", "label": "(held back)", "username": ""}, "suppressed", why)
                continue
            due.append(x)
        for x in due:
            for device in push.devices(store):
                try:
                    result = push.send(store, x["title"], x["body"], x["url"], tag=x["key"], only=device["id"])
                    status = ("sent" if result["sent"] else "failed" if result["failed"]
                              else "removed" if result["removed"] else "not_sent")
                    detail = "; ".join(result["failed"]) or ("subscription expired" if result["removed"] else "") \
                        or ("local mode" if result.get("local") else "")
                except Exception as e:
                    status, detail = "failed", f"{type(e).__name__}: {e}"
                store.alert_log_add(now, x, device, status, detail)
            sent[x["key"]] = now
            if x.get("kind"):
                dev = str(x["station"])
                radar_sent[dev] = [t for t in radar_sent.get(dev, []) if now - t < 3600] + [now]
            if x.get("kind") == "storm":
                e = x["evidence"]
                storms[str(x["station"])] = {"t": now, "track": e.get("track"), "dbz": e["dbz"], "posh": e["hail_probability"],
                                             "tvs": e["tornado_vortex_signature"], "eta": e["eta_seconds"]}
        week = lambda d: {k: v for k, v in d.items() if now - (v["t"] if isinstance(v, dict) else v) < 7 * 86400}
        store.set_meta(SENT_META, json.dumps(week(sent)))
        store.set_meta(STORMS_META, json.dumps(week(storms)))
        store.set_meta(RADAR_META, json.dumps({k: [t for t in v if now - t < 3600] for k, v in radar_sent.items()}))
        store.set_meta(NOTED_META, json.dumps({k: t for k, t in noted.items() if now - t < cooldown}))
        return due
