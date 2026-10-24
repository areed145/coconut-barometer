"""Satellite frames for the Weather map: which ones exist right now, so the browser can loop them. The tiles
themselves come straight from NASA GIBS (the page's CSP allows its host): GOES-East every 10 minutes, GeoColor
(true color by day) and clean infrared (cloud tops, day and night). GIBS publishes ~30-60 minutes behind real time.
The radar is weather/radar.py's.
"""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from .. import activity
from ..errors import caught

GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best"
SATELLITE = {                                       # name -> (GIBS layer, tile matrix set, native max zoom)
    "geocolor": ("GOES-East_ABI_GeoColor", "GoogleMapsCompatible_Level7", 7),
    "infrared": ("GOES-East_ABI_Band13_Clean_Infrared", "GoogleMapsCompatible_Level6", 6),
}
SAT_FRAMES, SAT_LOOKBACK = 9, 4 * 3600              # up to 90 minutes of 10-minute frames, from the last four hours


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def expand_periods(domain: str) -> list[int]:
    """'start/end/PT10M,start/end/PT10M' (WMTS time domain) -> every time in it, as epoch seconds."""
    out = []
    for part in filter(None, domain.split(",")):
        bits = part.strip().split("/")
        start = end = _epoch(bits[0])
        step = 600
        if len(bits) == 3:
            end = _epoch(bits[1])
            m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", bits[2])
            step = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60) if m else 600
        out += range(start, end + 1, max(60, step))
    return sorted(set(out))


def _in_background(fn) -> None:
    threading.Thread(target=fn, name="imagery", daemon=True).start()


class Imagery:
    """The current frame lists, fetched at most once per `ttl` seconds. Past that, the saved lists are
    still answered at once while a refresh runs in the background (`spawn`); only with nothing saved, or
    lists older than `stale`, does a request wait for the providers. They're asked all at once. A
    provider that fails gives an empty list (and its error), so the map still shows whatever else is up."""

    def __init__(self, get=requests.get, ttl: int = 240, stale: int = 900, clock=time.time, spawn=_in_background):
        self._get, self._ttl, self._stale, self._clock, self._spawn = get, ttl, stale, clock, spawn
        self._lock = threading.Lock()          # guards _cached and _refreshing
        self._fetching = threading.Lock()      # one round of provider calls at a time
        self._cached: tuple[float, dict] | None = None
        self._refreshing = False

    def frames(self) -> dict:
        with self._lock:
            cached, age = self._cached, self._clock() - self._cached[0] if self._cached else None
            if cached and age < self._ttl:
                return cached[1]
            if cached and age < self._stale:
                if not self._refreshing:
                    self._refreshing = True
                    self._spawn(self._refresh)
                return cached[1]
        return self._refresh()

    def _refresh(self) -> dict:
        with self._fetching:
            with self._lock:                   # someone else may have refreshed while this waited
                if self._cached and self._clock() - self._cached[0] < self._ttl:
                    self._refreshing = False
                    return self._cached[1]
            now = self._clock()
            try:
                with ThreadPoolExecutor(max_workers=len(SATELLITE)) as pool:
                    sats = {k: pool.submit(self._satellite, k, now) for k in SATELLITE}
                    out = {"satellite": {k: f.result() for k, f in sats.items()}}
                with self._lock:
                    self._cached = (now, out)
                return out
            finally:
                with self._lock:
                    self._refreshing = False

    def _satellite(self, name: str, now: float) -> dict:
        layer, tms, zoom = SATELLITE[name]
        url = f"{GIBS}/{layer}/default/{{time}}/{tms}/{{z}}/{{y}}/{{x}}.png"
        try:
            # Built by hand: GIBS rejects the time range with its ':' and '/' percent-encoded.
            with activity.log.call("imagery", f"satellite {name}", "NASA GIBS GOES-East") as a:
                r = self._get(f"{GIBS}/wmts.cgi?SERVICE=WMTS&REQUEST=DescribeDomains&LAYER={layer}&TILEMATRIXSET={tms}"
                              f"&time={_iso(now - SAT_LOOKBACK)}/{_iso(now)}", timeout=15)
                r.raise_for_status()
                m = re.search(r"<Domain>([^<]*)</Domain>", r.text)
                times = expand_periods(m.group(1)) if m else []
                a["detail"] = f"NASA GIBS GOES-East · {len(times)} frames"
        except Exception as e:  # noqa: BLE001
            caught(e, "satellite")
            return {"frames": [], "max_zoom": zoom, "url": url, "error": str(e)[:200]}
        return {"frames": [{"t": t, "time": _iso(t)} for t in times[-SAT_FRAMES:]], "max_zoom": zoom,
                "url": url, "error": None}
