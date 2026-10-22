"""Weather stations and their observations (WeatherFlow Tempest), all in metric as sent.
  wx_stations   a station: name, place, elevation, timezone
  wx_devices    its devices; only Tempests (device_type ST) report weather
  wx_obs        one row per device per report (1 minute), keyed by (device_id, ts)
  wx_events     lightning strikes and rain starting, from the live stream"""
from __future__ import annotations

import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from ..weather.parse import FIELDS, obs_st, sea_level
from .db import Database

# Seconds per history point: the smallest that keeps a chart under ~2000 points.
BUCKETS = (60, 300, 900, 1800, 3600, 10800, 21600, 86400)
OBS_COLS = ",".join(FIELDS)
SEA_LEVEL_SQL = ("CASE WHEN o.pressure IS NULL OR o.temp IS NULL OR COALESCE(s.elevation, 0) = 0 THEN o.pressure "
                 "ELSE o.pressure * pow(1 - 0.0065 * s.elevation / (o.temp + 0.0065 * s.elevation + 273.15), -5.257) END")
# Dew point (Magnus formula), and from it the estimated cloud base above the station: ~125 m per °C of spread.
# A station can't see the ceiling; this is the convective (cumulus) base, so it says nothing about layered overcast.
_MAGNUS = "(ln(NULLIF(o.humidity, 0) / 100) + 17.62 * o.temp / (243.12 + o.temp))"
DEW_SQL = f"(243.12 * {_MAGNUS} / (17.62 - {_MAGNUS}))"
CLOUD_BASE_SQL = f"greatest(0, (o.temp - {DEW_SQL}) * 125)"
# Feels like: the NWS heat index (Rothfusz, in °F) when hot and humid, the North American wind chill when cold
# and windy, otherwise the air temperature.
_F = "(o.temp * 1.8 + 32)"
_HEAT_INDEX = (f"((-42.379 + 2.04901523 * {_F} + 10.14333127 * o.humidity - 0.22475541 * {_F} * o.humidity "
               f"- 0.00683783 * {_F} * {_F} - 0.05481717 * o.humidity * o.humidity + 0.00122874 * {_F} * {_F} * o.humidity "
               f"+ 0.00085282 * {_F} * o.humidity * o.humidity - 0.00000199 * {_F} * {_F} * o.humidity * o.humidity) - 32) / 1.8")
_WIND_CHILL = "(13.12 + 0.6215 * o.temp - 11.37 * pow(o.wind_avg * 3.6, 0.16) + 0.3965 * o.temp * pow(o.wind_avg * 3.6, 0.16))"
FEELS_SQL = (f"CASE WHEN o.temp >= 27 AND o.humidity >= 40 THEN {_HEAT_INDEX} "
             f"WHEN o.temp <= 10 AND o.wind_avg * 3.6 > 4.8 THEN {_WIND_CHILL} ELSE o.temp END")
# Gust over average, skipping near-calm readings where the ratio means nothing.
GUST_FACTOR_SQL = "CASE WHEN o.wind_avg >= 1 THEN o.wind_gust / o.wind_avg END"
PARTS = {"night": (0, 6), "morning": (6, 12), "afternoon": (12, 18), "evening": (18, 24)}


class WeatherStore(Database):
    def wx_save_stations(self, stations: list[dict]) -> None:
        with self.db:
            for s in stations:
                self.db.execute("INSERT OR REPLACE INTO wx_stations VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (s["station_id"], s.get("name") or s.get("public_name") or str(s["station_id"]),
                                 s.get("latitude"), s.get("longitude"), s.get("station_meta", {}).get("elevation", s.get("elevation")),
                                 s.get("timezone"), int(time.time())))
                for d in s.get("devices") or []:
                    meta = d.get("device_meta") or {}
                    self.db.execute("INSERT OR REPLACE INTO wx_devices VALUES (?, ?, ?, ?, ?)",
                                    (d["device_id"], s["station_id"], d.get("serial_number"), d.get("device_type"),
                                     meta.get("name")))

    def wx_stations(self) -> list[dict]:
        """Stations that have a Tempest, each with its device_id, and how much has been stored for it."""
        rows = self.db.execute(
            "SELECT s.station_id, s.name, s.lat, s.lon, s.elevation, s.timezone, d.device_id, d.serial, "
            "(SELECT MIN(ts) FROM wx_obs o WHERE o.device_id = d.device_id) AS first_ts, "
            "(SELECT MAX(ts) FROM wx_obs o WHERE o.device_id = d.device_id) AS last_ts, "
            "(SELECT COUNT(*) FROM wx_obs o WHERE o.device_id = d.device_id) AS observations "
            "FROM wx_stations s JOIN wx_devices d ON d.station_id = s.station_id WHERE d.device_type = 'ST' "
            "ORDER BY s.name, d.device_id").fetchall()
        return [dict(r) for r in rows]

    def wx_devices(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT device_id, station_id FROM wx_devices WHERE device_type = 'ST' ORDER BY device_id")]

    def wx_range(self, device_id: int) -> tuple[int | None, int | None]:
        r = self.db.execute("SELECT MIN(ts), MAX(ts) FROM wx_obs WHERE device_id=?", (device_id,)).fetchone()
        return r[0], r[1]

    def wx_save_obs(self, device_id: int, arrays: list[list]) -> int:
        """Store obs_st arrays (REST history or a live message); a repeat of one replaces it. Returns how many."""
        rows = [o for o in (obs_st(a) for a in arrays) if o]
        if rows:
            with self.db:       # literals, many rows per statement: binding 19 parameters per row is far slower
                for i in range(0, len(rows), 500):
                    values = ",".join("(" + ",".join([str(int(device_id))] + [_num(r[f]) for f in FIELDS]) + ")"
                                      for r in rows[i:i + 500])
                    self.db.execute(f"INSERT OR REPLACE INTO wx_obs (device_id, {OBS_COLS}) VALUES {values}")
        return len(rows)

    def wx_save_event(self, device_id: int, ts: int, kind: str, value: float | None = None, energy: float | None = None):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO wx_events VALUES (?, ?, ?, ?, ?)", (device_id, ts, kind, value, energy))

    def wx_station(self, device_id: int) -> dict | None:
        return next((s for s in self.wx_stations() if s["device_id"] == device_id), None)

    def wx_latest(self, device_id: int) -> dict | None:
        """The newest observation with what the Live page adds: sea-level pressure and its 3-hour
        change, today's rain / high / low (local midnight), and the last lightning."""
        st = self.wx_station(device_id)
        if not st:
            return None
        row = self.db.execute(
            f"SELECT o.*, {SEA_LEVEL_SQL} AS pressure_sl FROM wx_obs o JOIN wx_devices d ON d.device_id = o.device_id "
            "JOIN wx_stations s ON s.station_id = d.station_id WHERE o.device_id=? ORDER BY ts DESC LIMIT 1",
            (device_id,)).fetchone()
        if not row:
            return {**st, "obs": None}
        obs = dict(row)
        past = self.db.execute(
            f"SELECT {SEA_LEVEL_SQL} AS p FROM wx_obs o JOIN wx_devices d ON d.device_id = o.device_id "
            "JOIN wx_stations s ON s.station_id = d.station_id WHERE o.device_id=? AND o.ts <= ? ORDER BY o.ts DESC LIMIT 1",
            (device_id, obs["ts"] - 3 * 3600)).fetchone()
        obs["pressure_change_3h"] = (obs["pressure_sl"] - past[0]) if past and past[0] is not None and obs["pressure_sl"] is not None else None
        midnight = local_midnight(st["timezone"], obs["ts"])
        day = self.db.execute("SELECT COALESCE(SUM(rain), 0), MAX(temp), MIN(temp), MAX(wind_gust) FROM wx_obs "
                              "WHERE device_id=? AND ts >= ?", (device_id, midnight)).fetchone()
        obs.update(rain_today=day[0], temp_high=day[1], temp_low=day[2], gust_high=day[3])
        strike = self.db.execute("SELECT ts, value FROM wx_events WHERE device_id=? AND kind='strike' ORDER BY ts DESC LIMIT 1",
                                 (device_id,)).fetchone()
        if not strike:
            strike = self.db.execute("SELECT ts, strike_dist FROM wx_obs WHERE device_id=? AND strike_count > 0 "
                                     "ORDER BY ts DESC LIMIT 1", (device_id,)).fetchone()
        obs["last_strike"] = {"ts": strike[0], "distance": strike[1]} if strike else None
        return {**st, "obs": obs}

    def wx_history(self, device_ids: list[int], start: int, end: int) -> dict:
        """Averages (and the extremes that matter) per time bucket, for charts. The bucket grows with the
        range. {bucket, t: [...], devices: {id: {field: [... aligned to t, None where no data]}}}."""
        bucket = next((b for b in BUCKETS if (end - start) / b <= 2000), BUCKETS[-1])
        out: dict[int, dict[int, dict]] = {}
        for dev in device_ids:
            rows = self.db.execute(
                f"""SELECT (o.ts // {bucket}) * {bucket} AS t, AVG(o.temp) AS temp, MIN(o.temp) AS temp_min, MAX(o.temp) AS temp_max,
                    AVG({FEELS_SQL}) AS feels_like, AVG({GUST_FACTOR_SQL}) AS gust_factor,
                    AVG(o.humidity) AS humidity, AVG({DEW_SQL}) AS dew_point, MIN({CLOUD_BASE_SQL}) AS cloud_base,
                    AVG({SEA_LEVEL_SQL}) AS pressure,
                    AVG(o.wind_avg) AS wind_avg, MAX(o.wind_gust) AS wind_gust, MIN(o.wind_lull) AS wind_lull,
                    degrees(atan2(AVG(sin(radians(o.wind_dir))), AVG(cos(radians(o.wind_dir))))) AS wind_dir,
                    SUM(o.rain) AS rain, AVG(o.solar) AS solar, MAX(o.uv) AS uv, AVG(o.illuminance) AS illuminance,
                    SUM(o.strike_count) AS strikes, AVG(o.battery) AS battery
                    FROM wx_obs o JOIN wx_devices d ON d.device_id = o.device_id
                    JOIN wx_stations s ON s.station_id = d.station_id
                    WHERE o.device_id = ? AND o.ts >= ? AND o.ts <= ? GROUP BY 1 ORDER BY 1""",
                (dev, start, end)).fetchall()
            out[dev] = {r["t"]: dict(r) for r in rows}
        ts = sorted({t for d in out.values() for t in d})
        fields = ("temp", "temp_min", "temp_max", "feels_like", "humidity", "dew_point", "cloud_base", "pressure",
                  "wind_avg", "wind_gust", "wind_lull", "gust_factor",
                  "wind_dir", "rain", "solar", "uv", "illuminance", "strikes", "battery")
        devices = {}
        for dev, by_t in out.items():
            cols = {f: [(by_t[t][f] if t in by_t else None) for t in ts] for f in fields}
            cols["wind_dir"] = [None if v is None else v % 360 for v in cols["wind_dir"]]
            _, lat, lon = self._place(dev)
            cols["clear_sky"] = [None if lat is None or lon is None else clear_sky_mean(lat, lon, t, bucket) for t in ts]
            devices[dev] = cols
        return {"bucket": bucket, "t": ts, "devices": devices}

    def _place(self, device_id: int) -> tuple[ZoneInfo, float | None, float | None]:
        """The station's timezone (UTC if unknown) and position."""
        row = self.db.execute("SELECT s.timezone, s.lat, s.lon FROM wx_devices d JOIN wx_stations s ON s.station_id = d.station_id "
                              "WHERE d.device_id=?", (device_id,)).fetchone()
        tz, lat, lon = row if row else (None, None, None)
        try:
            zone = ZoneInfo(tz) if tz else ZoneInfo("UTC")
        except Exception:
            zone = ZoneInfo("UTC")
        return zone, lat, lon

    def wx_days(self, device_id: int, start: int, end: int) -> dict:
        """By local day at the station, for the daily band, the hour-by-day heatmap and the calendar:
        {timezone, days: [{t: local midnight, date, low, high, mean, rain, uv, hours: [24 hourly mean temps]}]}.
        Temperatures are None on a day with none stored."""
        zone, _, _ = self._place(device_id)
        rows = self.db.execute("SELECT (ts // 3600) * 3600 AS t, AVG(temp), MIN(temp), MAX(temp), SUM(rain), MAX(uv) FROM wx_obs "
                               "WHERE device_id=? AND ts>=? AND ts<=? GROUP BY 1 ORDER BY 1",
                               (device_id, start, end)).fetchall()
        days: dict[str, dict] = {}
        for t, avg, lo, hi, rain, uv in rows:
            local = datetime.fromtimestamp(t, zone)
            d = days.setdefault(local.date().isoformat(), {
                "t": int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()),
                "low": None, "high": None, "rain": 0.0, "uv": None, "sum": [0.0] * 24, "n": [0] * 24})
            if avg is not None:
                d["low"] = lo if d["low"] is None else min(d["low"], lo)
                d["high"] = hi if d["high"] is None else max(d["high"], hi)
                d["sum"][local.hour] += avg          # the repeated hour when the clocks go back is averaged
                d["n"][local.hour] += 1
            d["rain"] += rain or 0.0
            if uv is not None:
                d["uv"] = uv if d["uv"] is None else max(d["uv"], uv)
        out = []
        for date, d in sorted(days.items()):
            hours = [s / n if n else None for s, n in zip(d["sum"], d["n"])]
            seen = [h for h in hours if h is not None]
            out.append({"t": d["t"], "date": date, "low": d["low"], "high": d["high"],
                        "mean": sum(seen) / len(seen) if seen else None, "rain": d["rain"], "uv": d["uv"], "hours": hours})
        return {"timezone": zone.key, "days": out}

    def wx_lightning(self, device_id: int, start: int, end: int, step: int = 1800) -> dict:
        """Strikes per `step`-second bin and kilometre of distance, for a heatmap. The station reports one
        average distance and a count per observation, so that's what is binned: cells = [[bin start, km, strikes], ...]."""
        rows = self.db.execute(
            f"SELECT (ts // {int(step)}) * {int(step)} AS t, CAST(round(strike_dist) AS INTEGER) AS km, SUM(strike_count) AS n "
            "FROM wx_obs WHERE device_id=? AND ts>=? AND ts<=? AND strike_count > 0 AND strike_dist IS NOT NULL "
            "GROUP BY 1, 2 ORDER BY 1, 2", (device_id, start, end)).fetchall()
        return {"step": step, "cells": [[r[0], max(0, r[1]), int(r[2])] for r in rows],
                "total": sum(int(r[2]) for r in rows)}

    def wx_wind_rose(self, device_id: int, start: int, end: int, part: str | None = None) -> dict:
        """Share of observations by 16 compass directions and wind-speed band (m/s). `part` keeps only one
        quarter of the station's local day (see PARTS)."""
        bands = (0, 2, 5, 8, 12, 999)
        rows = self.db.execute("SELECT ts, wind_dir, wind_avg FROM wx_obs WHERE device_id=? AND ts>=? AND ts<=? "
                               "AND wind_dir IS NOT NULL AND wind_avg IS NOT NULL", (device_id, start, end)).fetchall()
        if part:
            zone, (h0, h1), offsets = self._place(device_id)[0], PARTS[part], {}
            def hour(ts):                         # local hour, with the UTC offset looked up once per hour
                k = ts // 3600
                if k not in offsets:
                    offsets[k] = int(datetime.fromtimestamp(k * 3600, zone).utcoffset().total_seconds())
                return (ts + offsets[k]) // 3600 % 24
            rows = [r for r in rows if h0 <= hour(r[0]) < h1]
        grid = [[0] * (len(bands) - 1) for _ in range(16)]
        for _, d, v in rows:
            i = int(((d % 360) + 11.25) // 22.5) % 16
            j = next(k for k in range(len(bands) - 1) if v < bands[k + 1])
            grid[i][j] += 1
        return {"bands": bands[:-1], "counts": grid, "total": len(rows)}


def _num(v) -> str:
    """A number as an SQL literal (NULL for missing or not a finite number). Only numbers get through."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "NULL"
    return repr(f) if math.isfinite(f) else "NULL"


def clear_sky(lat: float, lon: float, ts: float) -> float:
    """Global horizontal irradiance under a cloudless sky (W/m², Haurwitz model), from the sun's height at
    that place and moment (NOAA's low-precision solar position)."""
    t = datetime.fromtimestamp(ts, ZoneInfo("UTC"))
    hour = t.hour + t.minute / 60 + t.second / 3600
    g = 2 * math.pi / 365 * (t.timetuple().tm_yday - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    ha = math.radians((hour * 60 + eqtime + 4 * lon) / 4 - 180)
    phi = math.radians(lat)
    cosz = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    return 1098 * cosz * math.exp(-0.057 / cosz) if cosz > 0 else 0.0


def clear_sky_mean(lat: float, lon: float, start: int, span: int) -> float:
    """clear_sky averaged over [start, start + span), sampled at most every 10 minutes (and at most 48 times)."""
    n = min(48, max(1, span // 600))
    return sum(clear_sky(lat, lon, start + (i + 0.5) * span / n) for i in range(n)) / n


def local_midnight(tz: str | None, ts: int) -> int:
    """Epoch of the most recent midnight at the station (UTC if its timezone is unknown)."""
    try:
        zone = ZoneInfo(tz) if tz else ZoneInfo("UTC")
    except Exception:
        zone = ZoneInfo("UTC")
    now = datetime.fromtimestamp(ts, zone)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
