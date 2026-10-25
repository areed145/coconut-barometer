"""RTMA at the stations, for station QC (#105): each analysis field read at a few points with small Range requests.

RTMA's fields are simple-packed with no bitmap (grib.py), so a value's bits sit at a known offset: the head of each
field's message (~200 bytes) gives the packing and the Lambert grid, and the two grid rows around a station
(~4 KB) give the four cells it's between, blended bilinearly. A whole analysis at two stations is about 25 small
requests, against ~6 MB a field for the map's (weather/rtma.py). The 15-minute rapid-update files are on AWS Open Data
back years, so the same reads also fill in the past (backfill).

Values are kept in the units QC compares: °C, hPa, m/s (u and v turned east and north), m, %."""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

from .. import grib
from ..lambert import Lambert
from ..rtma import url

# name -> (variable, level) in the analysis's .idx, and how its stored value becomes ours
FIELDS = {
    "hgt": ("HGT", "surface", lambda v: v),                        # the cell's terrain height (m)
    "pres": ("PRES", "surface", lambda v: v / 100),               # Pa -> hPa
    "t": ("TMP", "2 m above ground", lambda v: v - 273.15),       # K -> °C
    "td": ("DPT", "2 m above ground", lambda v: v - 273.15),
    "u": ("UGRD", "10 m above ground", lambda v: v),
    "v": ("VGRD", "10 m above ground", lambda v: v),
    "gust": ("GUST", "10 m above ground", lambda v: v),
    "tcdc": ("TCDC", "entire atmosphere (considered as a single layer)", lambda v: v),   # cloud cover, %
}
HEAD = 1024              # bytes read for a field's head (its sections before the values are ~184)
WORKERS = 4


def offsets(idx: str) -> dict[str, int]:
    """An analysis's .idx -> FIELDS name -> where its message starts."""
    wanted = {(var, level): name for name, (var, level, _) in FIELDS.items()}
    out = {}
    for line in idx.splitlines():
        parts = line.split(":")
        if len(parts) >= 6 and parts[1].isdigit() and (parts[3], parts[4]) in wanted:
            out[wanted[(parts[3], parts[4])]] = int(parts[1])
    return out


def cells(lam: Lambert, lat: float, lon: float) -> tuple[list[int], list[float]] | None:
    """The four cells around (lat, lon), as indexes in the file's order, and their bilinear weights; None off the grid."""
    nx, ny = lam.grid["nx"], lam.grid["ny"]
    i, j = (float(v) for v in lam.ij(lat, lon))
    i0, j0 = math.floor(i), math.floor(j)
    if not (0 <= i0 < nx - 1 and 0 <= j0 < ny - 1):
        return None
    fi, fj = i - i0, j - j0
    idx = [j0 * nx + i0, j0 * nx + i0 + 1, (j0 + 1) * nx + i0, (j0 + 1) * nx + i0 + 1]
    return idx, [(1 - fi) * (1 - fj), fi * (1 - fj), (1 - fi) * fj, fi * fj]


class RtmaPoints:
    def __init__(self, get=requests.get):
        self._get = get

    def exists(self, t: int) -> str | None:
        """The analysis at `t`'s .idx text, or None when there's none (not out yet, or a gap)."""
        r = self._get(url(t) + ".idx", timeout=20)
        if r.status_code in (403, 404):
            return None
        r.raise_for_status()
        return r.text

    def _range(self, t: int, first: int, last: int) -> bytes:
        r = self._get(url(t), headers={"Range": f"bytes={first}-{last}"}, timeout=30)
        r.raise_for_status()
        return r.content

    def _field(self, t: int, name: str, start: int, places: list[tuple[float, float]]) -> tuple[int, list[float | None]]:
        h = grib.head(self._range(t, start, start + HEAD - 1))
        lam = Lambert(h["grid"])
        out = []
        for lat, lon in places:
            found = cells(lam, lat, lon)
            if found is None:
                out.append(None)
                continue
            idx, w = found
            first, last = grib.point_bytes(h, min(idx), max(idx))
            data = self._range(t, start + first, start + last)
            v = grib.point_values(h, data, first, idx)
            out.append(float(np.dot(w, v)))
        if name in ("u", "v") and h["grid"]["grid_relative"]:
            out = [(v, lam, lon) for v, (_, lon) in zip(out, places)]     # turned once both are read: sample()
        return h["t"], out

    def sample(self, t: int, places: list[tuple[float, float]], idx: str | None = None) -> dict | None:
        """The analysis at `t` at each (lat, lon): {t (its valid time), points: [{hgt, pres, t, td, u, v, gust, tcdc}
        or None off the grid]}; None when there's no such analysis."""
        idx = idx if idx is not None else self.exists(t)
        if idx is None:
            return None
        starts = offsets(idx)
        missing = set(FIELDS) - set(starts)
        if missing:
            raise ValueError(f"not in the analysis: {', '.join(sorted(missing))}")
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            got = dict(zip(FIELDS, pool.map(lambda n: self._field(t, n, starts[n], places), FIELDS)))
        valid = got["t"][0]
        points = []
        for k, (lat, lon) in enumerate(places):
            if got["t"][1][k] is None:
                points.append(None)
                continue
            p = {name: FIELDS[name][2](got[name][1][k]) for name in FIELDS if name not in ("u", "v")}
            u, v = got["u"][1][k], got["v"][1][k]
            if isinstance(u, tuple):                         # grid-relative: east and north at the station
                (u, lam, lon_), (v, _, _) = u, v
                eu, ev = lam.earth_relative(np.array([u]), np.array([v]), lon_)
                u, v = float(eu[0]), float(ev[0])
            p["u"], p["v"] = u, v
            points.append(p)
        return {"t": valid, "points": points}
