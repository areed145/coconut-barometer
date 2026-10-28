"""Station QC's lightning check (#109): the Tempest's strikes against GOES-East GLM's flashes around the station.

The Tempest gives only a distance (its sensor's estimate to the storm's edge, in coarse steps from 1 to 40 km); GLM
gives each flash's place (sampler.py keeps those within REACH_KM, with distance and bearing). Each strike is matched
with a GLM flash within MATCH_S seconds at about its distance (DIST_KM, or DIST_SHARE of it, whichever is more):

    hit rate         strikes GLM confirms
    false alarms     strikes with no GLM flash anywhere in reach within QUIET_S either side: no storm at all, so
                     interference (a motor, a fence charger, a vibration)
    missed storms    BIN_S periods with at least STORM_FLASHES GLM flashes within MISS_KM in which the station heard
                     nothing: a sensor gone deaf
    distance bias    the station's distance minus GLM's, over the matched strikes; each gets GLM's bearing too

Only strikes GLM was watched around (MATCH_S either side, sampler.py's coverage spans, #159) are judged: the station's
history reaches back past when GLM was first kept, and the sampler isn't running through a restart; a strike then isn't
interference, it's just unknown (counted apart, "uncovered"). A storm period still counts as heard by any strike.

GLM sees in-cloud flashes too and misses some (more by day), and the Tempest hears through the cloud, so neither
rate is expected near 100%: the thresholds are loose."""
from __future__ import annotations

import bisect

MATCH_S = 60            # a GLM flash's time is its 20-second file's start; a strike from history, its minute's
DIST_KM, DIST_SHARE = 5.0, 0.4
QUIET_S = 600
MISS_KM, BIN_S, STORM_FLASHES = 20.0, 900, 3
MIN_STRIKES, MIN_STORMS = 10, 3        # fewer than these: not enough to judge by
KEEP_MATCHED = 20                      # the newest matched strikes kept with their bearing


def _flags(times: list[int], what: str, join: int = QUIET_S) -> list[tuple]:
    out: list[list[int]] = []
    for t in times:
        if out and t - out[-1][1] <= join:
            out[-1][1] = t
        else:
            out.append([t, t])
    return [(a, b, what) for a, b in out]


def lightning(store, station, start, end, cfg) -> dict:
    q = cfg["qc"]
    cover = store.qc_glm_cover(start - MATCH_S, end + MATCH_S)
    every = store.qc_strikes(station["device_id"], start, end)
    strikes = [s for s in every if any(a <= s[0] - MATCH_S and s[0] + MATCH_S <= b for a, b in cover)]
    flashes = store.qc_glm_flashes(station["device_id"], start - QUIET_S, end + QUIET_S)
    times = [f[0] for f in flashes]

    matched, unconfirmed = [], []
    for ts, km in strikes:
        lo, hi = bisect.bisect_left(times, ts - QUIET_S), bisect.bisect_right(times, ts + QUIET_S)
        if lo == hi:
            unconfirmed.append(ts)
            continue
        near = [f for f in flashes[lo:hi] if abs(f[0] - ts) <= MATCH_S and abs(f[1] - km) <= max(DIST_KM, DIST_SHARE * km)]
        if near:
            f = min(near, key=lambda f: abs(f[1] - km))
            matched.append((ts, km, f[1], f[2]))

    storms, missed = 0, []
    bins: dict[int, int] = {}
    for ts, km, _ in flashes:
        if start <= ts <= end and km <= MISS_KM:
            bins[ts // BIN_S] = bins.get(ts // BIN_S, 0) + 1
    heard = {ts // BIN_S for ts, _ in every}
    for b, n in sorted(bins.items()):
        if n >= STORM_FLASHES:
            storms += 1
            if b not in heard:
                missed.append(b * BIN_S)

    n = len(strikes)
    hit = len(matched) / n if n else None
    false = len(unconfirmed) / n if n else None
    miss = len(missed) / storms if storms else None
    bias = sum(s - g for _, s, g, _ in matched) / len(matched) if matched else None
    metrics = {"strikes": n, "uncovered": len(every) - n, "matched": len(matched), "unconfirmed": len(unconfirmed),
               "storms": storms, "missed": len(missed),
               "hit_rate": None if hit is None else round(hit, 3), "false_rate": None if false is None else round(false, 3),
               "miss_rate": None if miss is None else round(miss, 3), "distance_bias": None if bias is None else round(bias, 2),
               "newest_matched": [{"ts": ts, "km": km, "glm_km": g, "bearing": b} for ts, km, g, b in matched[-KEEP_MATCHED:]],
               "show": [["strikes", n, "count"], ["confirmed by GLM", len(matched), "count"],
                        ["nearby storm periods", storms, "count"]]}
    if len(every) > n:
        metrics["show"].append(["strikes outside GLM coverage, not judged", len(every) - n, "count"])
    flags = _flags(unconfirmed, "strikes with no lightning in reach") + [(t, t + BIN_S, "lightning nearby, none heard") for t in missed]
    judged = []
    if n >= MIN_STRIKES:
        judged.append(0 if false < q["lightning_false_warn"] else 1 if false < q["lightning_false_fail"] else 2)
        metrics["show"] = [[f"confirmed by GLM ({len(matched)} of {n})", hit * 100, "pct"],
                           ["with no lightning in reach", false * 100, "pct"]]
        if bias is not None:
            metrics["show"].append(["distance against GLM", bias, "km"])
    if storms >= MIN_STORMS:
        judged.append(0 if miss < q["lightning_miss_warn"] else 1 if miss < q["lightning_miss_fail"] else 2)
        metrics["show"] = [*(metrics["show"] if n >= MIN_STRIKES else []), ["of nearby storm periods missed", miss * 100, "pct"]]
    if not judged:
        return {"status": "insufficient", "metrics": metrics, "flags": flags}
    return {"status": ("pass", "warn", "fail")[max(judged)], "metrics": metrics, "flags": flags}


SUITES = {"lightning": ("Lightning vs GLM", lightning)}
