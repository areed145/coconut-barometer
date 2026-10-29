"""Rain totals for the Weather map (#177): NOAA MRMS's multi-sensor QPE, the rain that fell over the last 1, 3, 6, 12,
24, 48 or 72 hours (a product apiece), from the radars corrected with rain gauges. Pass 1, up ~15 minutes after each
hour (Pass 2 waits for more gauges, an hour). Public domain, from NCEP's MRMS server with no key: the `latest` file,
fetched only when it has changed (If-Modified-Since), 0.5-7 MB. A 0.01° grid over the US, PNG-packed GRIB2 as the
radar's is (radar.decode(), grid()), in mm, -3 where no radar sees.

Drawn as the radar is (#179): each 2×2 block averaged (~2 km; what no radar sees counts as dry), totals under the
lowest of BANDS and specks dropped (radar.clean()), then smoothed and traced into stacked shapes per BANDS level
(radar.encode(), view()) at two LEVELS of detail, coarser than the radar's: a total is smooth, and covers far more of
the map than echo does (72 hours of it, ~170 KB gzipped for the US, ~250 KB a view zoomed in). A Feed per period
(feed.py), made when the map first asks for it and refreshed in the background, one tracing at a time (~2 s and
~100 MB each; Totals, one per process: `qpe`)."""
from __future__ import annotations

import gzip
import threading
import time

import numpy as np

from . import radar
from .feed import Feed
from .rtma import TTL

HOURS = (1, 3, 6, 12, 24, 48, 72)
# The levels traced, in mm: inches from 0.01 to 10, about evenly by ratio, so the colors step finely at any total.
BANDS = [round(i * 25.4, 3) for i in (0.01, 0.02, 0.05, 0.1, 0.15, 0.25, 0.35, 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8, 10)]
# radar.LEVELS's shape: name -> (Gaussian sigma in ~2 km pixels, traced on every n-th, whole 1/q degrees). Fine (~4 km)
# from FINE_ZOOM in, for the view; coarse (~10 km) the whole US. The zoom is rtma.py's finest, as the map asks for both.
LEVELS = {"fine": (2.0, 2, 250), "coarse": (3.0, 5, 100)}
FINE_ZOOM = 8
BLOCK, SPECK_PX = 2, 4   # the grid averaged in 2×2 blocks (~2 km); rain over fewer of those, connected, is a speck
ROWS = 100               # the grid's rows averaged at a time (a multiple of BLOCK)
_tracing = threading.Lock()     # one period traced at a time, however many the map asks for at once


def url(hours: int) -> str:
    product = f"MultiSensor_QPE_{hours:02d}H_Pass1"
    return f"https://mrms.ncep.noaa.gov/2D/{product}/MRMS_{product}.latest.grib2.gz"


def field(msg: dict, raw: np.ndarray) -> dict:
    """radar.decode()'s message and its stored values (north at the top) -> the total in BLOCK×BLOCK means (mm,
    float32, north at the top; unseen: 0) and where it is, as radar.crop() has a field."""
    ref, e, d = (np.float32(x) for x in msg["scale"])
    b = BLOCK
    ny, nx = raw.shape[0] // b, raw.shape[1] // b
    rows = []
    for r0 in range(0, ny * b, ROWS):
        mm = (raw[r0:min(r0 + ROWS, ny * b), :nx * b].astype(np.float32) * e + ref) / d
        np.maximum(mm, 0, out=mm)
        rows.append(mm.reshape(mm.shape[0] // b, b, nx, b).mean(axis=(1, 3), dtype=np.float32))
    return {"north": msg["la1"] - (b - 1) / 2 * msg["dj"], "west": msg["lo1"] + (b - 1) / 2 * msg["di"],
            "di": b * msg["di"], "dj": b * msg["dj"], "mm": np.concatenate(rows)}


def shapes(msg: dict, raw: np.ndarray) -> dict:
    """The total's stacked shapes at each of LEVELS: {level: [[mm, polys], ...]}."""
    f = field(msg, raw)
    mm = radar.clean(f.pop("mm"), SPECK_PX, floor=BANDS[0])
    return {name: radar.encode(f, mm, *spec, bands=BANDS) for name, spec in LEVELS.items()}


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
        with _tracing:
            levels = shapes(msg, radar.grid(msg))
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

    def view(self, hours: int, bbox: tuple | None = None, zoom: float | None = None) -> tuple[dict, str]:
        """The rain over the `hours` up to `valid`: ({hours, valid, checked_at, error, pending}, the shapes for the
        box at the zoom's level as radar.view()'s JSON text, each band's level its mm)."""
        state = self.feed(hours).current()
        data = state["data"]
        rest = {"hours": hours, "valid": data and data["valid"], **{k: state[k] for k in ("checked_at", "error", "pending")}}
        return rest, radar.view(data, bbox, zoom, "mm", LEVELS, FINE_ZOOM) if data else '{"level":null,"q":1,"bands":[]}'


qpe = Totals()
