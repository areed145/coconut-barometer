"""Alerts (Settings → Alerts): what's worth a notification, checked every CHECK_EVERY seconds from what the
streams have already stored, and sent with push.py to the devices turned on.

    lightning   a strike within alerts.lightning_km of a station (the live stream's strike events, and the
                strike count in observations, for when the stream missed one)
    gust        a gust over alerts.gust_kmh
    pressure    sea-level pressure down alerts.pressure_drop_hpa or more over 3 hours (the newest observation
                against the one 3 hours before it, as the Live card shows)
    callsign    a callsign from Settings → APRS heard after alerts.callsign_quiet_hours without a packet
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

from . import push
from .weather import radar as rd

CHECK_EVERY = 60
STALE = 15 * 60          # pressure: only from an observation this fresh
SENT_META = "alerts_sent"
STORMS_META, RADAR_META, NOTED_META = "alerts_storms", "alerts_radar", "alerts_noted"
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


COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _minutes(s: float) -> str:
    return "now" if s < 90 else f"~{round(s / 60)} min"


class Alerts:
    def __init__(self, cfg: dict, callsigns=lambda: [], radar=None):
        self.cfg, self.callsigns, self.radar = cfg, callsigns, radar
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
        return out

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
