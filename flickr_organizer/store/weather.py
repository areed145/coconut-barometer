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
                    AVG(o.humidity) AS humidity, AVG({SEA_LEVEL_SQL}) AS pressure,
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
        fields = ("temp", "temp_min", "temp_max", "humidity", "pressure", "wind_avg", "wind_gust", "wind_lull",
                  "wind_dir", "rain", "solar", "uv", "illuminance", "strikes", "battery")
        devices = {}
        for dev, by_t in out.items():
            cols = {f: [(by_t[t][f] if t in by_t else None) for t in ts] for f in fields}
            cols["wind_dir"] = [None if v is None else v % 360 for v in cols["wind_dir"]]
            devices[dev] = cols
        return {"bucket": bucket, "t": ts, "devices": devices}

    def wx_days(self, device_id: int, start: int, end: int) -> dict:
        """Temperature by local day at the station, for the daily band and the hour-by-day heatmap:
        {timezone, days: [{t: local midnight, date, low, high, mean, hours: [24 hourly means, None where no data]}]}."""
        row = self.db.execute("SELECT s.timezone FROM wx_devices d JOIN wx_stations s ON s.station_id = d.station_id "
                              "WHERE d.device_id=?", (device_id,)).fetchone()
        tz = row[0] if row else None
        try:
            zone = ZoneInfo(tz) if tz else ZoneInfo("UTC")
        except Exception:
            zone = ZoneInfo("UTC")
        rows = self.db.execute("SELECT (ts // 3600) * 3600 AS t, AVG(temp), MIN(temp), MAX(temp) FROM wx_obs "
                               "WHERE device_id=? AND ts>=? AND ts<=? AND temp IS NOT NULL GROUP BY 1 ORDER BY 1",
                               (device_id, start, end)).fetchall()
        days: dict[str, dict] = {}
        for t, avg, lo, hi in rows:
            local = datetime.fromtimestamp(t, zone)
            d = days.setdefault(local.date().isoformat(), {
                "t": int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()),
                "low": lo, "high": hi, "sum": [0.0] * 24, "n": [0] * 24})
            d["low"], d["high"] = min(d["low"], lo), max(d["high"], hi)
            d["sum"][local.hour] += avg          # the repeated hour when the clocks go back is averaged
            d["n"][local.hour] += 1
        out = []
        for date, d in sorted(days.items()):
            hours = [s / n if n else None for s, n in zip(d["sum"], d["n"])]
            seen = [h for h in hours if h is not None]
            out.append({"t": d["t"], "date": date, "low": d["low"], "high": d["high"],
                        "mean": sum(seen) / len(seen), "hours": hours})
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

    def wx_wind_rose(self, device_id: int, start: int, end: int) -> dict:
        """Share of observations by 16 compass directions and wind-speed band (m/s)."""
        bands = (0, 2, 5, 8, 12, 999)
        rows = self.db.execute("SELECT wind_dir, wind_avg FROM wx_obs WHERE device_id=? AND ts>=? AND ts<=? "
                               "AND wind_dir IS NOT NULL AND wind_avg IS NOT NULL", (device_id, start, end)).fetchall()
        grid = [[0] * (len(bands) - 1) for _ in range(16)]
        for d, v in rows:
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


def local_midnight(tz: str | None, ts: int) -> int:
    """Epoch of the most recent midnight at the station (UTC if its timezone is unknown)."""
    try:
        zone = ZoneInfo(tz) if tz else ZoneInfo("UTC")
    except Exception:
        zone = ZoneInfo("UTC")
    now = datetime.fromtimestamp(ts, zone)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
