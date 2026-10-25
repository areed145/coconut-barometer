"""Station QC's checks (#105): each a function from a station and a time range to a status and its numbers.

A check returns {status, metrics}: status is pass, warn, fail, or insufficient (not enough to go on), and metrics are
its numbers, with `show`: the few that sum it up, as [label, value, kind] for the scorecard to put in the viewer's
units (kind: tempd a temperature difference, tempspread one with no sign (an RMSE), temp, hpa, ms, deg, pct, m,
count, ratio, number, signed). A check may also return `flags`: (start, end, what) for the periods it found. Thresholds
come from Settings → Weather → Station QC (cfg["qc"]). run() runs every check for every station and keeps the results
and flags.

The checks, added issue by issue (#106-#112, #122-#123):
    temperature   the station's temperature against RTMA's at its cell, allowing for the height between them
    range, steps, stuck, coverage, solar, battery   the station's own readings (internal.py, #112)"""
from __future__ import annotations

import math
import time

from . import internal

LAPSE = 0.0065               # °C per m: the standard atmosphere's, for the height between the station and RTMA's cell


def _status(n: int, bad: float, warn: float, fail: float, need: int) -> str:
    if n < need:
        return "insufficient"
    return "fail" if bad >= fail else "warn" if bad >= warn else "pass"


def temperature(store, station: dict, start: int, end: int, cfg: dict) -> dict:
    """The station's temperature minus RTMA's at its cell, RTMA's moved to the station's height by the standard lapse
    rate (the cell's terrain is an average over 2.5 km): its mean (bias), RMSE and spread."""
    q = cfg["qc"]
    elev = station.get("elevation")
    diffs, heights = [], []
    for _, t, hgt, temp in store.qc_temperature_pairs(station["device_id"], start, end):
        dz = (elev - hgt) if elev is not None and hgt is not None else 0.0
        diffs.append(temp - (t - LAPSE * dz))
        heights.append(dz)
    n = len(diffs)
    if not n:
        return {"status": "insufficient", "metrics": {"n": 0, "show": [["readings compared", 0, "count"]]}}
    bias = sum(diffs) / n
    rmse = math.sqrt(sum(d * d for d in diffs) / n)
    sd = math.sqrt(max(0.0, rmse * rmse - bias * bias))
    dz = sum(heights) / n
    return {"status": _status(n, abs(bias), q["temp_bias_warn"], q["temp_bias_fail"], q["min_pairs"]),
            "metrics": {"n": n, "bias": round(bias, 3), "rmse": round(rmse, 3), "sd": round(sd, 3), "height_m": round(dz, 1),
                        "show": [["vs RTMA", round(bias, 2), "tempd"], ["RMSE", round(rmse, 2), "tempspread"],
                                 ["readings compared", n, "count"]]}}


# name -> (title, check), in the order the scorecard shows them
SUITES = {"temperature": ("Temperature vs RTMA", temperature), **internal.SUITES}


def run(store, cfg: dict, now: float | None = None) -> list[dict]:
    """Every check for every station over the last cfg["qc"]["window_days"], kept (qc_runs): what each said."""
    now = int(now or time.time())
    start = now - int(cfg["qc"]["window_days"] * 86400)
    out = []
    for station in store.qc_stations():
        for name, (_, check) in SUITES.items():
            result = check(store, station, start, now, cfg)
            store.qc_save_run(station["device_id"], name, now, start, now, result)
            store.qc_save_flags(station["device_id"], name, start, now, result.get("flags") or [])
            out.append({"device_id": station["device_id"], "suite": name, **{k: v for k, v in result.items() if k != "flags"},
                        "flags": len(result.get("flags") or [])})
    return out
