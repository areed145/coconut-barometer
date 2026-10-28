"""Rain totals for the Weather map (#177): NOAA MRMS's multi-sensor QPE, the rain that fell over the last 1, 3, 6, 12,
24, 48 or 72 hours (a product apiece), from the radars corrected with rain gauges. Pass 1, up ~15 minutes after each
hour (Pass 2 waits for more gauges, an hour). Public domain, from NCEP's MRMS server with no key: the `latest` file,
fetched only when it has changed (If-Modified-Since), 0.5-7 MB. A 0.01° grid over the US, PNG-packed GRIB2 as the
radar's is (radar.decode(), grid()), in mm, -3 where no radar sees.

Each is averaged once onto LEVELS regular lat/lon grids (sent from the same zooms as rtma.py's, so the map asks for
both alike), in bands of rows to keep the memory down, and kept as int16 tenths of a mm: a block's mean of what's seen
in it, MISSING where nothing is. rtma.cut() cuts the view. A Feed per period (feed.py), made when the map first asks
for it and refreshed in the background (Totals; one per process, `qpe`)."""
from __future__ import annotations

import gzip
import threading
import time

import numpy as np

from . import radar
from .feed import Feed
from .rtma import TTL, cut, level_for, pack

HOURS = (1, 3, 6, 12, 24, 48, 72)
# name -> (degrees between points, the map's lowest zoom for it): rtma.py's zooms, finer, since rain's edges are sharp.
LEVELS = {"coarse": (0.1, 0), "mid": (0.05, 6), "fine": (0.02, 8)}
BAND = 100               # the grid's rows averaged at a time (a multiple of each level's block)


def url(hours: int) -> str:
    product = f"MultiSensor_QPE_{hours:02d}H_Pass1"
    return f"https://mrms.ncep.noaa.gov/2D/{product}/MRMS_{product}.latest.grib2.gz"


def average(msg: dict, raw: np.ndarray) -> dict:
    """radar.decode()'s message and its stored values (north at the top) -> each level's {box, p}: mm, int16 tenths,
    rows south first, each point the mean of what's seen in its block (MISSING: nothing seen)."""
    ref, e, d = (np.float32(x) for x in msg["scale"])
    out = {}
    for level, (step, _) in LEVELS.items():
        b = round(step / msg["di"])
        ny, nx = raw.shape[0] // b, raw.shape[1] // b
        rows = []
        for r0 in range(0, ny * b, BAND):
            mm = (raw[r0:min(r0 + BAND, ny * b), :nx * b].astype(np.float32) * e + ref) / d
            seen = mm >= 0
            k = mm.shape[0] // b
            total = np.where(seen, mm, 0).reshape(k, b, nx, b).sum(axis=(1, 3))
            n = seen.reshape(k, b, nx, b).sum(axis=(1, 3))
            rows.append(pack(np.where(n > 0, total / np.maximum(n, 1, dtype=np.float32), np.float32(np.nan))))
        box = {"lat0": round(msg["la1"] - ((ny - 1) * b + (b - 1) / 2) * msg["dj"], 6),
               "lon0": round(msg["lo1"] + (b - 1) / 2 * msg["di"], 6),
               "dlat": round(b * msg["dj"], 6), "dlon": round(b * msg["di"], 6), "ny": ny, "nx": nx}
        out[level] = {"box": box, "p": np.ascontiguousarray(np.concatenate(rows)[::-1])}
    return out


class Qpe(Feed):
    source, via, ttl = "imagery", "NOAA MRMS", TTL

    def __init__(self, hours: int, *a, **k):
        k.setdefault("background", True)
        super().__init__(*a, **k)
        self.hours, self.what = hours, f"rain totals ({hours} h)"

    def load(self, a: dict) -> dict | None:
        r = self._get(url(self.hours), headers=self.conditional(), timeout=60)
        if r.status_code == 304:
            return None
        r.raise_for_status()
        msg = radar.decode(gzip.decompress(r.content))
        size = len(r.content)
        levels = average(msg, radar.grid(msg))
        self.remember(r)
        a["detail"] = f"{self.hours} h to {time.strftime('%H:%MZ', time.gmtime(msg['t']))}, {size / 1e6:.1f} MB"
        return {"valid": msg["t"], "levels": levels}


class Totals:
    """A Qpe per period, made when it's first asked for."""

    def __init__(self, **feed):
        self._feed, self._feeds, self._lock = feed, {}, threading.Lock()

    def feed(self, hours: int) -> Qpe:
        with self._lock:
            if hours not in self._feeds:
                self._feeds[hours] = Qpe(hours, **self._feed)
            return self._feeds[hours]

    def view(self, hours: int, bbox: tuple | None = None, zoom: float | None = None) -> dict:
        """The rain over the `hours` up to `valid` (p: mm, int16 tenths, base64, little-endian, rows south first,
        MISSING where no radar sees) for the box at the zoom's level: rtma.Rtma.view()'s shape, and `hours`."""
        state = self.feed(hours).current()
        out = {"hours": hours, "valid": None, "level": None, "fields": {},
               **{k: state[k] for k in ("checked_at", "error", "pending")}}
        if not state["data"]:
            return out
        return {**out, **cut(state["data"], ("p",), bbox, level_for(zoom, LEVELS))}


qpe = Totals()
