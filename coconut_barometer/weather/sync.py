"""History from REST. One request per day of observations (1-minute data, ~1440 rows), so a week
is 7 requests and a year ~365. Forward from the newest stored observation, then backwards (newest
day first) to weatherflow.backfill_days. Raising that setting later fills the older days on the next run."""
from __future__ import annotations

import time

DAY = 86400
MIN_SPAN = 3600                 # don't ask for less than an hour at the edge of the window
EMPTY_DAYS_BEFORE_FLOOR = 3     # this many empty days in a row going back: the device wasn't installed yet


def refresh_stations(client, store) -> list[dict]:
    stations = client.stations()
    store.wx_save_stations(stations)
    return stations


def fill(client, store, device_id: int, backfill_days: int, now: float | None = None, log=lambda *a: None) -> dict:
    """Fetch what's missing for one device. Returns {rows, calls}."""
    now = int(now or time.time())
    calls0, rows = client.call_count, 0
    first, last = store.wx_range(device_id)
    # Forward: from the newest stored up to now (a restart, or the stream was down).
    t = (last + 1) if last else now
    while t < now:
        rows += store.wx_save_obs(device_id, client.device_obs(device_id, t, min(t + DAY - 1, now)))
        t += DAY
    # Backwards, newest day first: older than the oldest stored (or from now, the first time), down to the
    # backfill start or to where the device began.
    want = now - backfill_days * DAY
    began = store.get_meta(f"wx_floor_{device_id}")      # set once a walk back found nothing older
    top = first if first else now + 1
    empty = 0
    while not began and top - want > MIN_SPAN:
        lo = max(top - DAY, want)
        got = store.wx_save_obs(device_id, client.device_obs(device_id, lo, top - 1))
        rows += got
        empty = 0 if got else empty + 1
        top = lo
        if empty >= EMPTY_DAYS_BEFORE_FLOOR:
            store.set_meta(f"wx_floor_{device_id}", top)       # the device has nothing older: stop asking
            log(f"weather: device {device_id} has nothing before {top}")
            break
    return {"rows": rows, "calls": client.call_count - calls0}


def sync(client, store, backfill_days: int, now: float | None = None, log=lambda *a: None) -> dict:
    """Stations first (names, locations, which devices there are), then every Tempest's history."""
    calls0 = client.call_count
    stations = refresh_stations(client, store)
    out = {"stations": len(stations), "rows": 0, "calls": 0}
    for dev in store.wx_devices():
        got = fill(client, store, dev["device_id"], backfill_days, now, log)
        out["rows"] += got["rows"]
    out["calls"] = client.call_count - calls0
    return out
