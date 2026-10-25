"""Station QC's checks against RTMA at the station's cell (#106; wind #107 and pressure #110 to come). Each pairs an
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
                 from any steady offset the siting gives it"""
from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

LAPSE, DEW_LAPSE = 0.0065, 0.0018      # °C per m: temperature (standard atmosphere), and dewpoint
SOLAR_BINS = (0, 10, 200, 400, 600, 800)          # W/m²: night, then by sunshine
WIND_BINS = (0, 1, 2, 4)                          # m/s
SUNNY, CALM, NIGHT = 600, 2.0, 10                 # strong sun (W/m²) with light wind (m/s); night: under NIGHT W/m²
SUN_PAIRS = 8                                     # each of sunny-and-calm and night needs this many to judge by
DRIFT_DAYS = 7


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


def differences(store, station: dict, start: int, end: int) -> list[dict]:
    """Each RTMA sample with the station around it: {ts, hour (local), solar, wind, dt (station - RTMA °C, height
    adjusted), dd (dewpoint)}; read once per run (kept on `station`)."""
    key = ("_rtma", start, end)
    if station.get("_rkey") == key:
        return station["_rtma"]
    elev, zone, out = station.get("elevation"), _zone(station), []
    for p in store.qc_rtma_pairs(station["device_id"], start, end):
        dz = (elev - p["hgt"]) if elev is not None and p["hgt"] is not None else 0.0
        dt = p["s_temp"] - (p["t"] - LAPSE * dz) if p["s_temp"] is not None and p["t"] is not None else None
        dd = p["s_dew"] - (p["td"] - DEW_LAPSE * dz) if p["s_dew"] is not None and p["td"] is not None else None
        out.append({"ts": p["ts"], "hour": datetime.fromtimestamp(p["ts"], zone).hour, "solar": p["s_solar"],
                    "wind": p["s_wind"], "dz": dz, "dt": dt, "dd": dd})
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


SUITES = {"temperature": ("Temperature vs RTMA", temperature), "dewpoint": ("Dewpoint vs RTMA", dewpoint),
          "sun": ("Sun on the sensor", sun), "drift": ("Temperature drift", drift)}
