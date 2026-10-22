"""Radar and satellite frames for the Weather map: which ones exist right now, so the browser can
loop them. The tiles themselves come straight from the providers (the page's CSP allows their hosts).

- Radar: NOAA NEXRAD base reflectivity, a US composite every 5 minutes, from the Iowa Environmental
  Mesonet's tile cache. It keeps the last hour as separate layers (…-m05m … -m55m).
- Satellite: GOES-East from NASA GIBS, every 10 minutes: GeoColor (true color by day) and clean
  infrared (cloud tops, day and night). GIBS publishes ~30-60 minutes behind real time.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone

import requests

from .. import activity

IEM = "https://mesonet.agron.iastate.edu"
GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best"
RADAR_STEP, RADAR_FRAMES = 300, 12                  # the last hour
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


class Imagery:
    """The current frame lists, fetched at most once per `ttl` seconds. A provider that fails gives an
    empty list (and its error), so the map still shows whatever else is up."""

    def __init__(self, get=requests.get, ttl: int = 240, clock=time.time):
        self._get, self._ttl, self._clock = get, ttl, clock
        self._lock = threading.Lock()
        self._cached: tuple[float, dict] | None = None

    def frames(self) -> dict:
        with self._lock:
            now = self._clock()
            if self._cached and now - self._cached[0] < self._ttl:
                return self._cached[1]
            out = {"radar": self._radar(), "satellite": {k: self._satellite(k, now) for k in SATELLITE}}
            self._cached = (now, out)
            return out

    def _radar(self) -> dict:
        try:
            with activity.log.call("imagery", "radar frames", "IEM NEXRAD composite") as a:
                meta = self._get(f"{IEM}/data/gis/images/4326/USCOMP/n0q_0.json", timeout=10).json()["meta"]
                valid = _epoch(meta["valid"])
                a["detail"] = f"IEM NEXRAD composite · newest {meta['valid']}"
        except Exception as e:  # noqa: BLE001 - any failure just means no radar this time
            return {"frames": [], "error": str(e)[:200]}
        frames = [{"t": valid - k * RADAR_STEP, "layer": "nexrad-n0q-900913" + (f"-m{k * 5:02d}m" if k else "")}
                  for k in range(RADAR_FRAMES - 1, -1, -1)]
        return {"frames": frames, "url": f"{IEM}/cache/tile.py/1.0.0/{{layer}}/{{z}}/{{x}}/{{y}}.png", "error": None}

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
            return {"frames": [], "max_zoom": zoom, "url": url, "error": str(e)[:200]}
        return {"frames": [{"t": t, "time": _iso(t)} for t in times[-SAT_FRAMES:]], "max_zoom": zoom,
                "url": url, "error": None}
