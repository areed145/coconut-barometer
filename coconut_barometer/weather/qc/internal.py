"""Station QC's internal checks (#112): what a station's own readings say about it, no outside data needed (except
RTMA's wind and cloud cover, to tell a stuck vane from a calm day and a dirty sensor from a cloudy one).

    range     readings outside what's physically possible (MADIS's level 1 validity limits, and solar, UV and rain)
    steps     changes too big: more in an hour than MADIS's temporal limits (not wind's), or a one-minute spike that
              goes back
    stuck     a sensor reporting the same value too long (STUCK_HOURS, set above the longest our healthy stations do:
              at 0.1 resolution, a calm night has held one temperature for 4 h), the vane at 0 while RTMA has wind, or
              solar at 0 on a day the sun was up
    coverage  the share of the minutes reported, and the gaps
    solar     on clear days (RTMA's cloud cover under CLEAR_PCT around midday), the day's peak against the clear-sky
              model: low means a dirty or shaded sensor, high a miscalibrated one
    battery   the lowest voltage of the last week, against the Tempest's power-saving thresholds

Each returns its status and numbers (suites.py) and flags: (start, end, what) for the periods it found, kept for the
scorecard and for History (#113)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from ...store.weather import clear_sky

# MADIS level 1 (madis.ncep.noaa.gov/madis_sfc_qc_notes.shtml), in our units: -60..130 °F, dewpoint -90..90 °F,
# station pressure 568..1100 mb, wind 0..250 kt; and solar, UV and a minute's rain, generously.
LIMITS = {"temp": (-51.1, 54.4), "dew": (-67.8, 32.2), "humidity": (0, 100), "pressure": (568, 1100),
          "wind_avg": (0, 128.6), "wind_gust": (0, 128.6), "solar": (0, 1800), "uv": (0, 20), "rain": (0, 40)}
# MADIS's temporal check: the most a value may change in an hour (35 °F, 15 mb). Not its wind limit (20 kt): that's for
# hourly reports, and on one-minute readings a thunderstorm's outflow passes it (3 to 12 m/s in minutes, on ours).
PER_HOUR = {"temp": 19.4, "dew": 19.4, "pressure": 15.0}
SPIKE = {"temp": 3.0, "pressure": 2.0}          # a one-minute jump at least this big that's back within SPIKE_BACK
SPIKE_BACK = 0.3                                # ... to within this share of it, the next minute
STUCK_HOURS = {"temp": 8, "pressure": 6, "humidity": 24}
CALM_HOURS, WINDY = 6, 3.0                       # the vane at 0 this long while RTMA's wind averaged this much (m/s)
DAYLIGHT = 300                                  # W/m² of clear sky: the sun's well up
CLEAR_PCT, MIDDAY = 15, (10, 14)                # a clear day: RTMA's cloud cover this low between these local hours
CLEAR_DAYS = 3                                  # fewer clear days than this: not enough to judge the solar sensor
BATTERY_WARN, BATTERY_FAIL = 2.41, 2.375        # V: the Tempest slows its sampling below these to save power
MAX_FLAGS = 50                                  # a check keeps at most this many flags (the longest)
DAY_READINGS = 100                              # a day with fewer readings than this isn't judged (solar)

COLUMNS = ("ts", "temp", "humidity", "pressure", "wind_avg", "wind_gust", "wind_dir", "solar", "uv", "rain", "battery",
           "report_interval")


def obs(store, station: dict, start: int, end: int) -> dict:
    """The station's observations in the window as arrays (with dewpoint), read once per run (kept on `station`)."""
    key = ("_obs", start, end)
    if station.get("_key") != key:
        rows = store.db.execute(f"SELECT {', '.join(COLUMNS)} FROM wx_obs WHERE device_id = ? AND ts BETWEEN ? AND ? ORDER BY ts",
                                (station["device_id"], start, end)).fetchall()
        a = {c: np.array([r[i] for r in rows], dtype=np.float64 if c != "ts" else np.int64) if rows else np.zeros(0)
             for i, c in enumerate(COLUMNS)}
        with np.errstate(divide="ignore", invalid="ignore"):
            g = np.log(np.where(a["humidity"] > 0, a["humidity"], np.nan) / 100) + 17.62 * a["temp"] / (243.12 + a["temp"])
            a["dew"] = 243.12 * g / (17.62 - g)
        station["_key"], station["_obs"] = key, a
    return station["_obs"]


def periods(mask: np.ndarray, ts: np.ndarray, join: int = 600) -> list[tuple[int, int]]:
    """Runs of True in `mask` as (first ts, last ts), runs less than `join` seconds apart joined."""
    out: list[list[int]] = []
    for t in ts[mask]:
        if out and t - out[-1][1] <= join:
            out[-1][1] = int(t)
        else:
            out.append([int(t), int(t)])
    return [tuple(p) for p in out]


def _flags(spans, what: str) -> list[tuple]:
    spans = sorted(spans, key=lambda p: p[1] - p[0], reverse=True)[:MAX_FLAGS]
    return [(a, b, what) for a, b in sorted(spans)]


def _insufficient(**show) -> dict:
    return {"status": "insufficient", "metrics": {"show": [[k.replace("_", " "), v, "count"] for k, v in show.items()]}, "flags": []}


# ---- the checks ---------------------------------------------------------------------------------------------------
def value_range(store, station, start, end, cfg) -> dict:
    a = obs(store, station, start, end)
    n = len(a["ts"])
    if not n:
        return _insufficient(readings=0)
    bad, flags, by = np.zeros(n, bool), [], {}
    for name, (lo, hi) in LIMITS.items():
        v = a[name]
        out = ~np.isnan(v) & ((v < lo) | (v > hi))
        if out.any():
            by[name] = int(out.sum())
            bad |= out
            flags += _flags(periods(out, a["ts"]), f"{name} out of range")
    share = bad.sum() / n * 100
    status = "pass" if not bad.any() else "fail" if share > 1 else "warn"
    return {"status": status, "metrics": {"n": n, "bad": int(bad.sum()), "pct": round(float(share), 3), "by": by,
                                          "show": [["readings out of range", int(bad.sum()), "count"], ["of readings", float(share), "pct"]]},
            "flags": flags}


def steps(store, station, start, end, cfg) -> dict:
    a = obs(store, station, start, end)
    ts, n = a["ts"], len(a["ts"])
    if n < 120:
        return _insufficient(readings=n)
    flags, by = [], {}
    back = np.searchsorted(ts, ts - 3600)                 # the reading an hour before each (if there's one near it)
    near = np.abs(ts[np.minimum(back, n - 1)] - (ts - 3600)) <= 300
    for name, limit in PER_HOUR.items():
        v = a[name]
        d = np.abs(v - v[np.minimum(back, n - 1)])
        hit = near & ~np.isnan(d) & (d > limit)
        if hit.any():
            spans = periods(hit, ts)
            by[name] = len(spans)
            flags += _flags(spans, f"{name} changed more than {limit:g} in an hour")
    for name, size in SPIKE.items():
        v = a[name]
        if n < 3:
            continue
        jump, back_ = v[1:-1] - v[:-2], v[2:] - v[1:-1]
        quick = (ts[1:-1] - ts[:-2] <= 120) & (ts[2:] - ts[1:-1] <= 120)
        hit = quick & (np.abs(jump) >= size) & (np.abs(jump + back_) <= SPIKE_BACK * np.abs(jump))
        if hit.any():
            spans = periods(np.concatenate([[False], hit, [False]]), ts)
            by[f"{name}_spike"] = len(spans)
            flags += _flags(spans, f"{name} spike")
    events = sum(by.values())
    status = "pass" if not events else "fail" if events >= 10 else "warn"
    return {"status": status, "metrics": {"events": events, "by": by, "show": [["jumps or spikes", events, "count"]]}, "flags": flags}


def stuck(store, station, start, end, cfg) -> dict:
    a = obs(store, station, start, end)
    ts, n = a["ts"], len(a["ts"])
    if n < 120:
        return _insufficient(readings=n)
    spans_by: dict[str, list] = {}

    def same_runs(v, hours):
        out, i = [], 0
        while i < n:
            j = i
            while j + 1 < n and v[j + 1] == v[i] and ts[j + 1] - ts[j] <= 600:
                j += 1
            if not np.isnan(v[i]) and ts[j] - ts[i] >= hours * 3600:
                out.append((int(ts[i]), int(ts[j])))
            i = j + 1
        return out
    for name, hours in STUCK_HOURS.items():
        spans_by[name] = same_runs(a[name], hours)
    # The vane at 0 for CALM_HOURS while RTMA's wind (10 m) averaged WINDY or more over the same time.
    rt = store.db.execute("SELECT ts, sqrt(u * u + v * v) FROM qc_rtma WHERE device_id = ? AND ts BETWEEN ? AND ? AND u IS NOT NULL",
                          (station["device_id"], start, end)).fetchall()
    rts, rws = np.array([r[0] for r in rt], dtype=np.int64), np.array([r[1] for r in rt])
    calm = []
    for a0, b0 in same_runs(np.where(a["wind_avg"] == 0, 0.0, np.arange(n) + 1.0), CALM_HOURS):
        inside = (rts >= a0) & (rts <= b0)
        if inside.sum() >= 2 and rws[inside].mean() >= WINDY:
            calm.append((a0, b0))
    spans_by["wind"] = calm
    # Solar at 0 all day on a day the sun was well up.
    dark = []
    zone = _zone(station)
    days: dict = {}
    for t, s in zip(ts, a["solar"]):
        days.setdefault(datetime.fromtimestamp(int(t), zone).date(), []).append((int(t), s))
    for day, rows in days.items():
        if len(rows) < DAY_READINGS:
            continue
        noon = rows[len(rows) // 2][0]
        if clear_sky(station["lat"], station["lon"], noon) >= DAYLIGHT and all((s or 0) == 0 for _, s in rows):
            dark.append((rows[0][0], rows[-1][0]))
    spans_by["solar"] = dark
    flags = [f for name, spans in spans_by.items() for f in _flags(spans, f"{name} stuck")]
    ongoing = any(b >= int(ts[-1]) - 600 for spans in spans_by.values() for _, b in spans)
    found = sum(len(s) for s in spans_by.values())
    status = "pass" if not found else "fail" if ongoing else "warn"
    return {"status": status, "metrics": {"periods": found, "ongoing": ongoing, "by": {k: len(v) for k, v in spans_by.items() if v},
                                          "show": [["stuck periods", found, "count"]]}, "flags": flags}


def coverage(store, station, start, end, cfg) -> dict:
    """Minutes reported over the window from the station's first reading in it (a new station isn't missing the
    time before it was set up), to the end of the window (a station that's gone quiet is)."""
    a = obs(store, station, start, end)
    ts, n = a["ts"], len(a["ts"])
    if not n:
        return _insufficient(readings=0)
    q = cfg["qc"]
    every = int(np.nanmedian(a["report_interval"])) * 60 if not np.isnan(a["report_interval"]).all() else 60
    every = max(60, every)
    expected = max(1, (end - int(ts[0])) // every + 1)
    pct = min(100.0, n / expected * 100)
    gaps = [(int(ts[i]), int(ts[i + 1])) for i in np.nonzero(np.diff(ts) > 10 * 60)[0]]
    if end - int(ts[-1]) > 10 * 60:
        gaps.append((int(ts[-1]), end))
    longest = max((b - a_ for a_, b in gaps), default=0)
    status = "fail" if pct < q["coverage_fail"] else "warn" if pct < q["coverage_warn"] else "pass"
    return {"status": status, "metrics": {"pct": round(pct, 2), "gaps": len(gaps), "longest_h": round(longest / 3600, 2),
                                          "show": [["of minutes reported", pct, "pct"], ["gaps over 10 min", len(gaps), "count"]]},
            "flags": _flags(gaps, "no readings")}


def solar(store, station, start, end, cfg) -> dict:
    a = obs(store, station, start, end)
    if not len(a["ts"]):
        return _insufficient(clear_days=0)
    q, zone = cfg["qc"], _zone(station)
    cover = store.db.execute("SELECT ts, tcdc FROM qc_rtma WHERE device_id = ? AND ts BETWEEN ? AND ? AND tcdc IS NOT NULL",
                             (station["device_id"], start, end)).fetchall()
    midday: dict = {}
    for t, c in cover:
        local = datetime.fromtimestamp(t, zone)
        if MIDDAY[0] <= local.hour < MIDDAY[1]:
            midday.setdefault(local.date(), []).append(c)
    clear = {d for d, cs in midday.items() if len(cs) >= 2 and max(cs) <= CLEAR_PCT}
    ratios = []
    ts, s = a["ts"], a["solar"]
    for day in sorted(clear):
        d0 = int(datetime(day.year, day.month, day.day, tzinfo=zone).timestamp())
        inside = (ts >= d0) & (ts < d0 + 86400) & ~np.isnan(s)
        if inside.sum() < DAY_READINGS:
            continue
        # The station's best 10 minutes (one-minute peaks overshoot at cloud edges) against the model's peak.
        v = s[inside]
        k = max(1, round(600 / max(60, float(np.median(np.diff(ts[inside]))))))
        best = float(np.convolve(v, np.ones(k) / k, mode="valid").max()) if len(v) >= k else float(v.max())
        model = max(clear_sky(station["lat"], station["lon"], d0 + k * 600) for k in range(144))
        if model > 0:
            ratios.append(best / model)
    if len(ratios) < CLEAR_DAYS:
        return _insufficient(clear_days=len(ratios))
    ratio = float(np.median(ratios))
    status = "fail" if ratio < q["solar_fail"] else "warn" if ratio < q["solar_warn"] or ratio > 1.25 else "pass"
    return {"status": status, "metrics": {"ratio": round(ratio, 3), "clear_days": len(ratios),
                                          "show": [["of clear-sky peak", ratio * 100, "pct"], ["clear days", len(ratios), "count"]]},
            "flags": []}


def battery(store, station, start, end, cfg) -> dict:
    a = obs(store, station, start, end)
    ts, v = a["ts"], a["battery"]
    week = ~np.isnan(v) & (ts >= end - 7 * 86400) if len(ts) else np.zeros(0, bool)
    if not week.any():
        return _insufficient(readings=0)
    low = float(v[week].min())
    days = {}
    for t, x in zip(ts[~np.isnan(v)], v[~np.isnan(v)]):
        d = int(t) // 86400
        days[d] = min(days.get(d, x), x)
    slope = 0.0
    if len(days) >= 3:
        k = np.array(sorted(days))
        slope = float(np.polyfit(k, [days[i] for i in k], 1)[0] * 7)     # V a week, of the daily lows
    status = "fail" if low < BATTERY_FAIL else "warn" if low < BATTERY_WARN else "pass"
    flags = _flags(periods(~np.isnan(v) & (v < BATTERY_WARN), ts, join=3600), "battery low")
    return {"status": status, "metrics": {"low": round(low, 3), "per_week": round(slope, 4),
                                          "show": [["lowest this week (V)", round(low, 2), "number"],
                                                   ["a week, daily lows (V)", round(slope, 3), "signed"]]},
            "flags": flags}


def _zone(station: dict):
    try:
        return ZoneInfo(station.get("timezone") or "UTC")
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


SUITES = {"range": ("Values in range", value_range), "steps": ("Jumps and spikes", steps), "stuck": ("Stuck sensors", stuck),
          "coverage": ("Reporting", coverage), "solar": ("Solar vs clear sky", solar), "battery": ("Battery", battery)}
