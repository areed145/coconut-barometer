"""Surface temperature (#85) and wind (#86) for the Weather map: NOAA's RTMA Rapid Update, the Real-Time Mesoscale
Analysis, every 15 minutes. It blends the surface observations (METARs, mesonets) into a model's first guess on a
2.5 km grid over the US, terrain and coastlines included: the interpolated "now", done better than scattered stations
could be. Public domain, on AWS Open Data with no key (s3://noaa-rtma-pds).

Each analysis is one GRIB2 file (~84 MB) with a .idx listing where each field starts, so only what's drawn is fetched,
a Range request apiece: TMP and DPT (2 m) and UGRD and VGRD (10 m). They're simple-packed on a Lambert grid
(grib.py, lambert.py); the winds are along the grid's axes and are turned east and north. A file is up ~15-20 minutes
after its time, so the newest is found by asking for the .idx of each quarter hour back from now, newest first, and
nothing is fetched unless there's one newer than what's kept.

Each analysis is resampled once onto LEVELS regular lat/lon grids over the US, kept as int16 in tenths (°C, m/s, %):
relative humidity is derived from temperature and dew point. Coarse for the whole US zoomed out, finer for the view
zoomed in (view() cuts the box). The full grid isn't kept.
A Feed (feed.py): fetched when the map asks and what's kept is older than TTL, in the background (a few seconds
of downloading and decoding). One per process (`rtma`)."""
from __future__ import annotations

import base64
import math
import time

import numpy as np

from . import grib
from .feed import Feed
from .lambert import Lambert, latlon_grid, points, resample

BASE = "https://noaa-rtma-pds.s3.amazonaws.com/"
TTL = 5 * 60
EVERY, LOOK_BACK = 15 * 60, 8          # an analysis per quarter hour; how many quarters back to look
FIELDS = {"t": ("TMP", "2 m above ground"), "d": ("DPT", "2 m above ground"),
          "u": ("UGRD", "10 m above ground"), "v": ("VGRD", "10 m above ground")}
US = (20.0, 53.0, -130.0, -60.0)       # south, north, west, east: the RTMA CONUS grid, and a bit of sea
# name -> (degrees between points, the map's lowest zoom for it). A view picks the finest its zoom allows.
LEVELS = {"coarse": (0.25, 0), "mid": (0.1, 6), "fine": (0.05, 8)}
MISSING = -32768                       # int16 for no value (off the grid)


def url(t: int) -> str:
    day, hm = time.strftime("%Y%m%d", time.gmtime(t)), time.strftime("%H%M", time.gmtime(t))
    return f"{BASE}rtma2p5_ru.{day}/rtma2p5_ru.t{hm}z.2dvaranl_ndfd.grb2"


def ranges(idx: str, total: int | None = None) -> dict[str, tuple[int, int | None]]:
    """An .idx -> FIELDS name -> (first byte, last byte; None: to the end) of that field's message."""
    rows = []
    for line in idx.splitlines():
        parts = line.split(":")
        if len(parts) >= 6 and parts[1].isdigit():
            rows.append((int(parts[1]), parts[3], parts[4]))
    out = {}
    for k, (start, var, level) in enumerate(rows):
        for name, want in FIELDS.items():
            if (var, level) == want:
                end = rows[k + 1][0] - 1 if k + 1 < len(rows) else (total - 1 if total else None)
                out[name] = (start, end)
    missing = set(FIELDS) - set(out)
    if missing:
        raise ValueError(f"not in the analysis: {', '.join(sorted(missing))}")
    return out


def level_for(zoom: float | None) -> str:
    return max((k for k, (_, z) in LEVELS.items() if (zoom or 0) >= z), key=lambda k: LEVELS[k][1])


def pack(values: np.ndarray) -> np.ndarray:
    """float (°C or m/s; NaN: none) -> int16 tenths, MISSING for none."""
    out = np.round(np.nan_to_num(values, nan=MISSING / 10) * 10)
    return np.clip(out, MISSING, 32767).astype(np.int16)


class Rtma(Feed):
    source, what, via, ttl = "imagery", "surface analysis", "NOAA RTMA (AWS)", TTL

    def __init__(self, *a, **k):
        k.setdefault("background", True)
        super().__init__(*a, **k)

    def newest(self, after: int | None) -> tuple[int, str] | None:
        """The newest analysis's (time, .idx text) since `after`, looking back LOOK_BACK quarter hours."""
        start = int(self._clock()) // EVERY * EVERY
        for k in range(LOOK_BACK):
            t = start - k * EVERY
            if after is not None and t <= after:
                return None
            r = self._get(url(t) + ".idx", timeout=20)
            if r.status_code in (403, 404):
                continue
            r.raise_for_status()
            return t, r.text
        return None

    def load(self, a: dict) -> dict | None:
        kept = (self.data or {}).get("valid")
        found = self.newest(kept)
        if found is None:
            a["detail"] = "no newer analysis" if kept else "no analysis in the last two hours"
            return None
        t, idx = found
        boxes = {level: latlon_grid(*US, step) for level, (step, _) in LEVELS.items()}
        got, size, valid, lam, relative = {}, 0, t, None, False
        for name, (start, end) in ranges(idx).items():          # one full field in memory at a time
            r = self._get(url(t), headers={"Range": f"bytes={start}-{'' if end is None else end}"}, timeout=60)
            r.raise_for_status()
            size += len(r.content)
            (msg,) = grib.messages(r.content)
            del r
            lam = lam or Lambert(msg["grid"])
            valid, relative = msg["t"], relative or (name in "uv" and msg["grid"]["grid_relative"])
            full = grib.values(msg)
            del msg
            # Each level averages over what's between its points (a Gaussian of about half its spacing).
            got[name] = {level: resample(lam, full, box, max(0.0, LEVELS[level][0] / 0.025 / 2 - 0.5)) for level, box in boxes.items()}
            del full
        levels = {}
        for level, box in boxes.items():
            u, v = got["u"][level], got["v"][level]
            if relative:
                u, v = lam.earth_relative(u, v, points(box)[1])
            t, d = got["t"][level] - np.float32(273.15), got["d"][level] - np.float32(273.15)
            rh = 100 * np.exp(17.625 * d / (243.04 + d) - 17.625 * t / (243.04 + t))
            levels[level] = {"box": box, "t": pack(t), "d": pack(d), "rh": pack(np.clip(rh, 0, 100)),
                             "u": pack(u), "v": pack(v)}
        a["detail"] = f"{time.strftime('%H:%MZ', time.gmtime(t))} analysis, {size / 1e6:.1f} MB"
        return {"valid": valid, "levels": levels}

    def view(self, names: tuple[str, ...], bbox: tuple | None = None, zoom: float | None = None) -> dict:
        """The newest analysis's `names` fields (t: °C, u, v: m/s eastward and northward, int16 tenths, base64,
        little-endian, rows south first, MISSING for none) for the box at the zoom's level: {valid, level, lat0,
        lon0, dlat, dlon, nx, ny, scale, missing, fields, checked_at, error, pending}."""
        state = self.current()
        out = {"valid": None, "level": None, "fields": {}, **{k: state[k] for k in ("checked_at", "error", "pending")}}
        data = state["data"]
        if not data:
            return out
        level = level_for(zoom)
        g = data["levels"][level]
        box = g["box"]
        r0, c0, r1, c1 = 0, 0, box["ny"], box["nx"]
        if bbox:                                   # the points covering the box (a hair's slack for float steps)
            w, s, e, n = bbox
            r0 = max(0, math.floor((s - box["lat0"]) / box["dlat"] + 1e-6))
            r1 = min(box["ny"], math.ceil((n - box["lat0"]) / box["dlat"] - 1e-6) + 1)
            c0 = max(0, math.floor((w - box["lon0"]) / box["dlon"] + 1e-6))
            c1 = min(box["nx"], math.ceil((e - box["lon0"]) / box["dlon"] - 1e-6) + 1)
        if r1 <= r0 or c1 <= c0:
            return {**out, "valid": data["valid"], "level": level}
        cut = {k: np.ascontiguousarray(g[k][r0:r1, c0:c1]) for k in names}
        return {**out, "valid": data["valid"], "level": level, "lat0": round(box["lat0"] + r0 * box["dlat"], 6),
                "lon0": round(box["lon0"] + c0 * box["dlon"], 6), "dlat": box["dlat"], "dlon": box["dlon"], "ny": r1 - r0, "nx": c1 - c0,
                "scale": 0.1, "missing": MISSING,
                "fields": {k: base64.b64encode(a.astype("<i2").tobytes()).decode() for k, a in cut.items()}}


rtma = Rtma()
