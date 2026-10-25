"""Is the igate working (#119)? Read from what it gated and the stream's own state, for the alert and Status.

The baseline is packets gated per hour for each hour of the week over the last 4 weeks: its median is what an hour
is expected to bring, its lowest the floor. (Under 2 weeks of history, the same hour of day over the last 7 days.)
Three things are told apart:

    APRS-IS down      the stream isn't connected (or only just reconnected): nothing can be said about the igate
    not gating        connected, but nothing gated for the quiet window when the baseline expects QUIET_RATE an hour
                      or more ("quiet"), or each of the last DEGRADED_HOURS hours under its floor ("degraded")
    beacon stopped    its own position beacon (meta aprs_igate_at) older than BEACON_HOURS"""
from __future__ import annotations

import statistics

QUIET_RATE = 10          # packets an hour the baseline must expect before silence means something
DEGRADED_HOURS = 3       # hours in a row under the floor
DEGRADED_MIN = 30        # ... that together expected at least this many
BEACON_HOURS = 3
WEEKS = 4


def baseline(counts: dict[int, int], first_hour: int, now_hour: int) -> dict[int, tuple[float, int]]:
    """{hour index: (median, lowest)} for the hours of the last day, from `counts` (hour index -> packets; an hour
    index is epoch seconds // 3600) since `first_hour`, the first hour with any data."""
    out = {}
    for h in range(now_hour - 24, now_hour + 1):
        weekly = [counts.get(h - 168 * w, 0) for w in range(1, WEEKS + 1) if h - 168 * w >= first_hour]
        daily = [counts.get(h - 24 * d, 0) for d in range(1, 8) if h - 24 * d >= first_hour]
        samples = weekly if len(weekly) >= 2 else daily if len(daily) >= 3 else []
        if samples:
            out[h] = (statistics.median(samples), min(samples))
    return out


def health(store, gate: str, now: int, stream: dict, quiet_minutes: int) -> dict:
    """The igate's state: `connected` (long enough to judge), `gating` (None, "quiet" or "degraded"), `beacon_stale`,
    and for Status the last packet, the last hour's count and what the baseline expected of it."""
    now_hour, window = now // 3600, max(quiet_minutes, 1) * 60
    counts = dict(store.db.execute("SELECT ts // 3600 AS h, COUNT(*) FROM aprs_igate WHERE igate=? AND ts >= ? AND ts <= ? "
                                   "GROUP BY 1", (gate, (now_hour - 168 * WEEKS) * 3600, now)).fetchall())
    first, last = store.db.execute("SELECT MIN(ts), MAX(ts) FROM aprs_igate WHERE igate=?", (gate,)).fetchone()
    last_hour = store.db.execute("SELECT COUNT(*) FROM aprs_igate WHERE igate=? AND ts > ? AND ts <= ?",
                                 (gate, now - 3600, now)).fetchone()[0]
    base = baseline(counts, first // 3600, now_hour) if first else {}
    up = (now - stream["since"]) if stream.get("state") == "connected" and stream.get("since") else 0
    beacon = store.igate_placed(gate)
    out = {"connected": up >= window, "gating": None, "beacon_stale": False, "last_ts": last, "last_hour": last_hour,
           "expected_hour": base[now_hour][0] if now_hour in base else None, "beacon_ts": beacon and beacon["ts"]}
    hours = [h for h in range((now - window) // 3600, now_hour + 1) if h in base]
    rate = statistics.mean(base[h][0] for h in hours) if hours else 0
    if out["connected"] and now - (last or 0) >= window and rate >= QUIET_RATE:
        out["gating"] = "quiet"
    elif up >= DEGRADED_HOURS * 3600:
        recent = [now_hour - i for i in range(1, DEGRADED_HOURS + 1)]
        if all(h in base and counts.get(h, 0) < base[h][1] for h in recent) and sum(base[h][0] for h in recent) >= DEGRADED_MIN:
            out["gating"] = "degraded"
    out["beacon_stale"] = bool(beacon and up >= BEACON_HOURS * 3600 and now - beacon["ts"] > BEACON_HOURS * 3600)
    out["rate"] = rate
    return out
