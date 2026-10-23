"""Alerts (Settings → Alerts): what's worth a notification, checked every CHECK_EVERY seconds from what the
streams have already stored, and sent with push.py to the devices turned on.

    lightning   a strike within alerts.lightning_km of a station (the live stream's strike events, and the
                strike count in observations, for when the stream missed one)
    gust        a gust over alerts.gust_kmh
    pressure    sea-level pressure down alerts.pressure_drop_hpa or more over 3 hours (the newest observation
                against the one 3 hours before it, as the Live card shows)
    callsign    a callsign from Settings → APRS heard after alerts.callsign_quiet_hours without a packet

Each looks only at what arrived since the last check (on startup, the last CHECK_EVERY), so a restart doesn't
replay old storms. The same alert for the same station or callsign is sent at most every
alerts.cooldown_minutes; when each was last sent is kept in the meta table, so a restart doesn't reset it."""
from __future__ import annotations

import json
import time

from . import push

CHECK_EVERY = 60
STALE = 15 * 60          # pressure: only from an observation this fresh
SENT_META = "alerts_sent"


def _dist(km: float, units: str) -> str:
    return f"{km * 0.621371:.0f} mi" if units == "imperial" else f"{km:.0f} km"


def _speed(ms: float, units: str) -> str:
    return f"{ms * 2.23694:.0f} mph" if units == "imperial" else f"{ms * 3.6:.0f} km/h"


def _pressure(hpa: float, units: str, digits: int = 1) -> str:
    return f"{hpa * 0.02953:.{digits + 1}f} inHg" if units == "imperial" else f"{hpa:.{digits}f} hPa"


def _hours(s: float) -> str:
    return f"{s / 86400:.0f} days" if s >= 2 * 86400 else f"{s / 3600:.0f} hours"


class Alerts:
    def __init__(self, cfg: dict, callsigns=lambda: []):
        self.cfg, self.callsigns = cfg, callsigns
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
                    out.append({"key": f"lightning:{dev}", "url": "/#/weather",
                                "title": f"Lightning {_dist(nearest, units)} from {name}",
                                "body": f"Strikes within {_dist(a['lightning_km'], units)} in the last few minutes."})
            if a["gust_kmh"]:
                gust = store.db.execute("SELECT MAX(wind_gust) FROM wx_obs WHERE device_id=? AND ts > ? AND ts <= ?",
                                        (dev, since, now)).fetchone()[0]
                if gust is not None and gust * 3.6 >= a["gust_kmh"]:
                    out.append({"key": f"gust:{dev}", "url": "/#/weather", "title": f"Gust of {_speed(gust, units)} at {name}",
                                "body": f"Over the {_speed(a['gust_kmh'] / 3.6, units)} you set."})
            if a["pressure_drop_hpa"]:
                o = (store.wx_latest(dev) or {}).get("obs")
                change = o and o.get("pressure_change_3h")
                if change is not None and o["ts"] > now - STALE and change <= -a["pressure_drop_hpa"]:
                    out.append({"key": f"pressure:{dev}", "url": "/#/weather",
                                "title": f"Pressure falling at {name}",
                                "body": f"Down {_pressure(-change, units)} in 3 hours, now {_pressure(o['pressure_sl'], units)}."})
        if a["callsign_quiet_hours"]:
            for call in self.callsigns():
                first = store.db.execute("SELECT MIN(ts) FROM aprs_positions WHERE callsign=? AND ts > ? AND ts <= ?",
                                         (call, since, now)).fetchone()[0]
                before = first and store.db.execute("SELECT MAX(ts) FROM aprs_positions WHERE callsign=? AND ts < ?",
                                                    (call, first)).fetchone()[0]
                if before and first - before >= a["callsign_quiet_hours"] * 3600:
                    out.append({"key": f"callsign:{call}", "url": "/#/aprs", "title": f"{call} heard",
                                "body": f"Its first packet in {_hours(first - before)}."})
        return out

    def run_once(self, store, now: float | None = None) -> list[dict]:
        """Check, and send what's due past its cool-down. Returns what was sent. Nothing is checked (or sent)
        while no device is turned on."""
        if not push.devices(store):
            self.since = None
            return []
        now = int(now or time.time())
        sent = json.loads(store.get_meta(SENT_META) or "{}")
        cooldown = self.cfg["alerts"]["cooldown_minutes"] * 60
        due = [x for x in self.check(store, now) if now - sent.get(x["key"], 0) >= cooldown]
        for x in due:
            push.send(store, x["title"], x["body"], x["url"], tag=x["key"])
            sent[x["key"]] = now
        if due:
            store.set_meta(SENT_META, json.dumps({k: t for k, t in sent.items() if now - t < 7 * 86400}))
        return due
