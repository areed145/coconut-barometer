"""Station QC's checks against RTMA at the station's cell (#106, wind #107, pressure #110). Each pairs an
RTMA sample with the station's readings around it (qc_rtma_pairs): the analysis is the 2.5 km grid's best estimate
from every observation and model nearby, so a lasting difference is the station (or its siting), not the weather.

    temperature  the station minus RTMA, RTMA moved to the station's height by the standard lapse rate (the cell's
                 terrain is an average over 2.5 km): mean (bias), RMSE, and the bias by local hour, sunshine and wind
    dewpoint     the same for dewpoint (from the station's humidity), which doesn't depend on the temperature's error
                 the way relative humidity does
    sun          the radiation shield and siting: the temperature bias in strong sun with little wind, over the bias
                 at night. Sunlight heating the sensor shows up there and goes away in wind; a sensor that's simply off
                 is off by the same day and night
    drift        the last DRIFT_DAYS' temperature bias against the rest of the window's: a sensor wandering off, apart
                 from any steady offset the siting gives it
    wind_dir     the station's wind direction minus RTMA's, when RTMA's wind is at least WIND_MIN: their circular mean
                 (one offset at every speed and direction is a station not aligned to north: the correction), and how
                 tightly they agree (the mean resultant length), the confidence in it
    wind_speed   the station's speed over RTMA's in SECTORS directions (where RTMA says it's from). A Tempest is
                 usually below 10 m, so its own overall ratio is the yardstick, not 1: a direction well under it is
                 something blocking the wind from that side
    pressure     the station's pressure minus RTMA's surface pressure moved to the station's elevation setting
                 (hypsometric, with RTMA's temperature): a steady offset is usually that setting being wrong (~1 hPa
                 per 8 m), not the sensor, so the elevation it implies is reported too. Also the sea-level pressures
                 against each other, and the last DRIFT_DAYS' offset against the rest of the window's"""
from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

from ..parse import sea_level

LAPSE, DEW_LAPSE = 0.0065, 0.0018      # °C per m: temperature (standard atmosphere), and dewpoint
SOLAR_BINS = (0, 10, 200, 400, 600, 800)          # W/m²: night, then by sunshine
WIND_BINS = (0, 1, 2, 4)                          # m/s
SUNNY, CALM, NIGHT = 600, 2.0, 10                 # strong sun (W/m²) with light wind (m/s); night: under NIGHT W/m²
SUN_PAIRS = 8                                     # each of sunny-and-calm and night needs this many to judge by
DRIFT_DAYS = 7
WIND_MIN = 3.0                                    # m/s of RTMA wind for a direction to compare
SECTORS = 16                                      # wind directions, 22.5° each
SECTOR_NAMES = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
G, RD = 9.80665, 287.05                           # gravity (m/s²), dry air's gas constant (J/kg/K)


def _zone(station):
    try:
        return ZoneInfo(station.get("timezone") or "UTC")
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _stats(d: list[float]) -> dict:
    n = len(d)
    if not n:
        return {"n": 0, "bias": None, "rmse": None}
    bias = sum(d) / n
    return {"n": n, "bias": round(bias, 3), "rmse": round(math.sqrt(sum(x * x for x in d) / n), 3)}


def _bin(edges, v) -> int:
    k = 0
    for i, e in enumerate(edges):
        if v >= e:
            k = i
    return k


def _from(east: float, north: float) -> float:
    """The direction a wind is from, in degrees, from its from-vector (east, north)."""
    return math.degrees(math.atan2(east, north)) % 360


def _turn(a: float) -> float:
    """An angle in [-180, 180)."""
    return (a + 180) % 360 - 180


def differences(store, station: dict, start: int, end: int) -> list[dict]:
    """Each RTMA sample with the station around it: {ts, hour (local), solar, wind, dt (station - RTMA °C, height
    adjusted), dd (dewpoint), r_speed and r_dir (RTMA's wind), s_dir (the station's, or None in calm), dp (station
    - RTMA hPa at the station's height), m_per_hpa (the height 1 hPa is there), dslp (sea-level pressures)}; read
    once per run (kept on `station`)."""
    key = ("_rtma", start, end)
    if station.get("_rkey") == key:
        return station["_rtma"]
    elev, zone, out = station.get("elevation"), _zone(station), []
    for p in store.qc_rtma_pairs(station["device_id"], start, end):
        dz = (elev - p["hgt"]) if elev is not None and p["hgt"] is not None else 0.0
        dt = p["s_temp"] - (p["t"] - LAPSE * dz) if p["s_temp"] is not None and p["t"] is not None else None
        dd = p["s_dew"] - (p["td"] - DEW_LAPSE * dz) if p["s_dew"] is not None and p["td"] is not None else None
        r_speed = r_dir = s_dir = dp = m_per_hpa = dslp = None
        if p["u"] is not None and p["v"] is not None:
            r_speed, r_dir = math.hypot(p["u"], p["v"]), _from(-p["u"], -p["v"])
        if p["s_wind_e"] is not None and p["s_wind_n"] is not None and math.hypot(p["s_wind_e"], p["s_wind_n"]) > 0.1:
            s_dir = _from(p["s_wind_e"], p["s_wind_n"])
        if p["pres"] is not None and p["s_pressure"] is not None:
            tm = (p["t"] if p["t"] is not None else 15.0) + 273.15 - LAPSE * dz / 2     # the layer between: its mean
            here = p["pres"] * math.exp(-G * dz / (RD * tm))
            dp, m_per_hpa = p["s_pressure"] - here, RD * tm / (G * here)
            if elev is not None and p["hgt"] is not None and p["t"] is not None and p["s_temp"] is not None:
                dslp = sea_level(p["s_pressure"], p["s_temp"], elev) - sea_level(p["pres"], p["t"], p["hgt"])
        out.append({"ts": p["ts"], "hour": datetime.fromtimestamp(p["ts"], zone).hour, "solar": p["s_solar"],
                    "wind": p["s_wind"], "dz": dz, "dt": dt, "dd": dd, "r_speed": r_speed, "r_dir": r_dir,
                    "s_dir": s_dir, "dp": dp, "m_per_hpa": m_per_hpa, "dslp": dslp})
    station["_rkey"], station["_rtma"] = key, out
    return out


def _status(n, bad, warn, fail, need) -> str:
    if n < need:
        return "insufficient"
    return "fail" if bad >= fail else "warn" if bad >= warn else "pass"


def _breakdown(rows, field) -> dict:
    """The bias by local hour, by sunshine and by wind: each {edges or hours, n, bias}."""
    def by(keyf, size):
        bins = [[] for _ in range(size)]
        for r in rows:
            k = keyf(r)
            if k is not None and r[field] is not None:
                bins[k].append(r[field])
        return {"n": [len(b) for b in bins], "bias": [round(sum(b) / len(b), 3) if b else None for b in bins]}
    return {"hour": by(lambda r: r["hour"], 24),
            "solar": {"edges": SOLAR_BINS, **by(lambda r: None if r["solar"] is None else _bin(SOLAR_BINS, r["solar"]), len(SOLAR_BINS))},
            "wind": {"edges": WIND_BINS, **by(lambda r: None if r["wind"] is None else _bin(WIND_BINS, r["wind"]), len(WIND_BINS))}}


def temperature(store, station, start, end, cfg) -> dict:
    q, rows = cfg["qc"], differences(store, station, start, end)
    d = [r["dt"] for r in rows if r["dt"] is not None]
    s = _stats(d)
    if not s["n"]:
        return {"status": "insufficient", "metrics": {"n": 0, "show": [["readings compared", 0, "count"]]}}
    dz = sum(r["dz"] for r in rows) / len(rows)
    return {"status": _status(s["n"], abs(s["bias"]), q["temp_bias_warn"], q["temp_bias_fail"], q["min_pairs"]),
            "metrics": {**s, "height_m": round(dz, 1), "by": _breakdown(rows, "dt"),
                        "show": [["vs RTMA", s["bias"], "tempd"], ["RMSE", s["rmse"], "tempspread"], ["readings compared", s["n"], "count"]]}}


def dewpoint(store, station, start, end, cfg) -> dict:
    q, rows = cfg["qc"], differences(store, station, start, end)
    s = _stats([r["dd"] for r in rows if r["dd"] is not None])
    if not s["n"]:
        return {"status": "insufficient", "metrics": {"n": 0, "show": [["readings compared", 0, "count"]]}}
    return {"status": _status(s["n"], abs(s["bias"]), q["dew_bias_warn"], q["dew_bias_fail"], q["min_pairs"]),
            "metrics": {**s, "by": _breakdown(rows, "dd"),
                        "show": [["vs RTMA", s["bias"], "tempd"], ["RMSE", s["rmse"], "tempspread"], ["readings compared", s["n"], "count"]]}}


def sun(store, station, start, end, cfg) -> dict:
    q, rows = cfg["qc"], differences(store, station, start, end)
    sunny = [r["dt"] for r in rows if r["dt"] is not None and (r["solar"] or 0) >= SUNNY and r["wind"] is not None and r["wind"] < CALM]
    night = [r["dt"] for r in rows if r["dt"] is not None and r["solar"] is not None and r["solar"] < NIGHT]
    if len(sunny) < SUN_PAIRS or len(night) < SUN_PAIRS:
        return {"status": "insufficient", "metrics": {"sunny": len(sunny), "night": len(night),
                                                      "show": [["sunny, calm readings", len(sunny), "count"], ["at night", len(night), "count"]]}}
    excess = sum(sunny) / len(sunny) - sum(night) / len(night)
    status = "fail" if excess >= q["sun_fail"] else "warn" if excess >= q["sun_warn"] else "pass"
    return {"status": status, "metrics": {"excess": round(excess, 3), "sunny": len(sunny), "night": len(night),
                                          "sunny_bias": round(sum(sunny) / len(sunny), 3), "night_bias": round(sum(night) / len(night), 3),
                                          "show": [["warmer in sun than at night", round(excess, 2), "tempd"],
                                                   ["sunny, calm readings", len(sunny), "count"]]}}


def drift(store, station, start, end, cfg) -> dict:
    q, rows = cfg["qc"], differences(store, station, start, end)
    cut = end - DRIFT_DAYS * 86400
    recent = [r["dt"] for r in rows if r["dt"] is not None and r["ts"] >= cut]
    before = [r["dt"] for r in rows if r["dt"] is not None and r["ts"] < cut]
    need = max(1, q["min_pairs"] // 2)
    if len(recent) < need or len(before) < need:
        return {"status": "insufficient", "metrics": {"recent": len(recent), "before": len(before),
                                                      "show": [["readings this week", len(recent), "count"], ["before", len(before), "count"]]}}
    change = sum(recent) / len(recent) - sum(before) / len(before)
    status = "fail" if abs(change) >= q["drift_fail"] else "warn" if abs(change) >= q["drift_warn"] else "pass"
    return {"status": status, "metrics": {"change": round(change, 3), "recent": len(recent), "before": len(before),
                                          "show": [[f"last {DRIFT_DAYS} days against before", round(change, 2), "tempd"]]}}


def wind_dir(store, station, start, end, cfg) -> dict:
    q, rows = cfg["qc"], differences(store, station, start, end)
    d = [math.radians(r["s_dir"] - r["r_dir"]) for r in rows
         if r["r_speed"] is not None and r["r_speed"] >= WIND_MIN and r["s_dir"] is not None]
    if len(d) < q["min_pairs"]:
        return {"status": "insufficient", "metrics": {"n": len(d), "show": [["windy readings compared", len(d), "count"]]}}
    sin_, cos_ = sum(map(math.sin, d)) / len(d), sum(map(math.cos, d)) / len(d)
    offset, agreement = _turn(math.degrees(math.atan2(sin_, cos_))), math.hypot(sin_, cos_)
    status = "fail" if abs(offset) >= q["wind_dir_fail"] else "warn" if abs(offset) >= q["wind_dir_warn"] else "pass"
    return {"status": status, "metrics": {"offset": round(offset, 1), "agreement": round(agreement, 3), "n": len(d),
                                          "show": [["vs RTMA", round(offset), "deg"], ["agreement", agreement * 100, "pct"],
                                                   ["windy readings compared", len(d), "count"]]}}


def wind_speed(store, station, start, end, cfg) -> dict:
    q, rows = cfg["qc"], differences(store, station, start, end)
    rows = [r for r in rows if r["r_speed"] is not None and r["r_speed"] >= WIND_MIN and r["wind"] is not None]
    if len(rows) < q["min_pairs"]:
        return {"status": "insufficient", "metrics": {"n": len(rows), "show": [["windy readings compared", len(rows), "count"]]}}
    overall = sum(r["wind"] for r in rows) / sum(r["r_speed"] for r in rows)
    station_, rtma, n = [0.0] * SECTORS, [0.0] * SECTORS, [0] * SECTORS
    for r in rows:
        k = int((r["r_dir"] + 180 / SECTORS) % 360 // (360 / SECTORS))
        station_[k] += r["wind"]
        rtma[k] += r["r_speed"]
        n[k] += 1
    need = max(4, q["min_pairs"] // SECTORS)            # a direction with fewer isn't judged
    ratio = [round(station_[k] / rtma[k], 3) if n[k] else None for k in range(SECTORS)]
    share = [ratio[k] / overall if n[k] >= need and overall > 0 else None for k in range(SECTORS)]
    judged = [k for k in range(SECTORS) if share[k] is not None]
    low = [k for k in judged if share[k] < q["wind_sector_warn"]]
    worst = min(judged, key=lambda k: share[k]) if judged else None
    status = ("insufficient" if not judged else "fail" if any(share[k] < q["wind_sector_fail"] for k in low)
              else "warn" if low else "pass")
    show = [["of RTMA's speed", overall, "ratio"]]
    if worst is not None:
        show.append([f"from the {SECTOR_NAMES[worst]}, against the rest", share[worst], "ratio"])
    return {"status": status, "metrics": {"ratio": round(overall, 3), "n": len(rows), "sectors": SECTOR_NAMES,
                                          "by_sector": {"n": n, "ratio": ratio},
                                          "low": [SECTOR_NAMES[k] for k in low], "show": show}}


def pressure(store, station, start, end, cfg) -> dict:
    q, rows = cfg["qc"], differences(store, station, start, end)
    rows = [r for r in rows if r["dp"] is not None]
    s = _stats([r["dp"] for r in rows])
    if s["n"] < q["min_pairs"]:
        return {"status": "insufficient", "metrics": {"n": s["n"], "show": [["readings compared", s["n"], "count"]]}}
    # The station's elevation setting is this much too high, if the barometer is right (a high setting moves RTMA
    # further down to it, so the station reads high against it)
    elevation = s["bias"] * sum(r["m_per_hpa"] for r in rows) / len(rows)
    slp = [r["dslp"] for r in rows if r["dslp"] is not None]
    cut = end - DRIFT_DAYS * 86400
    recent = [r["dp"] for r in rows if r["ts"] >= cut]
    before = [r["dp"] for r in rows if r["ts"] < cut]
    need = max(1, q["min_pairs"] // 2)
    change = (sum(recent) / len(recent) - sum(before) / len(before)) if len(recent) >= need and len(before) >= need else None
    worst = max(0 if abs(s["bias"]) < q["pressure_warn"] else 1 if abs(s["bias"]) < q["pressure_fail"] else 2,
                0 if change is None or abs(change) < q["pressure_drift_warn"] else 1 if abs(change) < q["pressure_drift_fail"] else 2)
    show = [["vs RTMA", s["bias"], "hpa"], ["in the elevation setting", elevation, "m"]]
    if change is not None:
        show.append([f"last {DRIFT_DAYS} days against before", change, "hpa"])
    return {"status": ("pass", "warn", "fail")[worst],
            "metrics": {**s, "elevation_m": round(elevation, 1), "height_m": round(sum(r["dz"] for r in rows) / len(rows), 1),
                        "sea_level_bias": round(sum(slp) / len(slp), 3) if slp else None,
                        "drift": round(change, 3) if change is not None else None, "show": show}}


SUITES = {"temperature": ("Temperature vs RTMA", temperature), "dewpoint": ("Dewpoint vs RTMA", dewpoint),
          "sun": ("Sun on the sensor", sun), "drift": ("Temperature drift", drift),
          "wind_dir": ("Wind direction vs RTMA", wind_dir), "wind_speed": ("Wind speed vs RTMA", wind_speed),
          "pressure": ("Pressure vs RTMA", pressure)}
