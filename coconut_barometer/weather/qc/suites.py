"""Station QC's checks (#105): each a function from a station and a time range to a status and its numbers.

A check returns {status, metrics}: status is pass, warn, fail, or insufficient (not enough to go on), and metrics are
its numbers, with `show`: the few that sum it up, as [label, value, kind] for the scorecard to put in the viewer's
units (kind: tempd a temperature difference, tempspread one with no sign (an RMSE), temp, hpa, ms, deg, pct, m, km,
count, ratio, number, signed). A check may also return `flags`: (start, end, what) for the periods it found. Thresholds
come from Settings → Weather → Station QC (cfg["qc"]). run() runs every check for every station and keeps the results
and flags.

The checks, added issue by issue (#106-#112, #122-#123):
    temperature, dewpoint, sun, drift                against RTMA at the station's cell (rtma_checks.py, #106)
    wind_dir, wind_speed                             the same, for wind (#107)
    pressure                                         the same, for pressure (#110)
    lightning                                        strikes against GOES-East GLM's flashes (lightning.py, #109)
    fronts                                           WPC fronts crossing the station (passages.py, #111)
    range, steps, stuck, coverage, solar, battery    the station's own readings (internal.py, #112)"""
from __future__ import annotations

import time

from . import internal, lightning, passages, rtma_checks

# name -> (title, check), in the order the scorecard shows them
SUITES = {**rtma_checks.SUITES, **lightning.SUITES, **passages.SUITES, **internal.SUITES}


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
