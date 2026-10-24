"""Radar (#54): smooth reflectivity shapes for the Weather map, nationwide storm cells, and what the
rain and storm alerts need (alerts.py).

- Reflectivity: NOAA MRMS's merged, quality-controlled composite (non-weather echoes already removed, every
  radar merged), every 2 minutes on a 0.01° (~1 km) grid over the US. The `latest` file is fetched only when it
  has changed (If-Modified-Since). Its GRIB2 data is a PNG, so Pillow decodes it, once, and it's used twice:
    the map    the whole US, each US_BLOCK×US_BLOCK block kept at its strongest (~2 km, so a core isn't averaged
               away), cleaned of specks, smoothed, and contoured (contourpy) on every US_THIN-th point (~4 km)
               into a filled polygon per BAND_STEP dBZ band. The browser colors the bands from Settings → Display
               → Radar colors. Shrinking first keeps the memory down (~150 MB at the peak, not ~500).
    alerts     MARGIN degrees around the stations at the full ~1 km, cleaned (not smoothed): the distance from
               each station to the nearest rain.
- Storm cells: the NWS radars' own storm tracking, as the Iowa Environmental Mesonet publishes it: every
  cell's position, motion, max dBZ, hail probability and rotation flags, every 5 minutes. `drct` is the
  direction a cell moves *from* (checked against successive scans), so it heads `drct` + 180.

Everything is held in memory: the latest frame, the latest cells, and each station's distance to the nearest
rain over the last half hour (how fast it shrinks is the rain alert's ETA). One per app (Runtime.radar)."""
from __future__ import annotations

import calendar
import gzip
import io
import math
import struct
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np
import requests

from .. import activity
from ..errors import caught

MRMS_URL = "https://mrms.ncep.noaa.gov/2D/MergedReflectivityQCComposite/MRMS_MergedReflectivityQCComposite.latest.grib2.gz"
CELLS_URL = "https://mesonet.agron.iastate.edu/geojson/nexrad_attr.py"
FRAME_EVERY, CELLS_EVERY, TICK = 120, 300, 20     # seconds
MARGIN = 3.0             # degrees around the stations (~330 km): the rain alert's reach
MIN_DBZ = 15             # weaker is left out (drizzle, and what's left of clutter)
SPECK_PX = 6             # connected echo smaller than this many ~1 km² pixels is dropped
SMOOTH = 1.5             # Gaussian sigma, in pixels, for the shapes (the alerts use the unsmoothed field) ...
THIN = 2                 # ... which are then traced on every THIN-th pixel: smooth anyway, and far fewer points
US_BLOCK, US_SPECK, US_SMOOTH, US_THIN = 2, 2, 1.0, 2   # the map's whole-US field: the same, on ~2 km pixels
MIN_RING = 4e-4          # deg² (~4 km²): smaller rings (specks, pinholes) are left out of the shapes
SAME_CELL_KM = 8         # cells this close are one storm seen by more than one radar
BAND_STEP, BAND_TOP = 5, 75
RAIN_DBZ, HERE_KM = 20, 3.0          # rain, and close enough to be raining at the station already
RAIN_HISTORY, RAIN_WINDOW = 20, 20 * 60
KM_PER_DEG = 111.2


# ---- MRMS ----------------------------------------------------------------------------------------------------
def decode(grib: bytes) -> dict:
    """One GRIB2 message (MRMS: a lat/lon grid, PNG-packed) -> {t, la1, lo1, di, dj, ni, nj, scale, png}."""
    if grib[:4] != b"GRIB" or grib[7] != 2:
        raise ValueError("not a GRIB2 file")
    out, i = {}, 16
    while grib[i:i + 4] != b"7777":
        n, sec = struct.unpack(">I", grib[i:i + 4])[0], grib[i + 4]
        s = grib[i:i + n]
        if sec == 1:
            y, mo, d, h, mi, se = struct.unpack(">H5B", s[12:19])
            out["t"] = calendar.timegm((y, mo, d, h, mi, se, 0, 0, 0))
        elif sec == 3:
            if struct.unpack(">H", s[12:14])[0] != 0:
                raise ValueError("not a lat/lon grid")
            ni, nj = struct.unpack(">II", s[30:38])
            la1, lo1 = struct.unpack(">iI", s[46:54])
            di, dj = struct.unpack(">II", s[63:71])
            out.update(ni=ni, nj=nj, la1=la1 / 1e6, lo1=(lo1 / 1e6 + 180) % 360 - 180, di=di / 1e6, dj=dj / 1e6)
        elif sec == 5:
            if struct.unpack(">H", s[9:11])[0] != 41:
                raise ValueError("not PNG-packed")
            ref = struct.unpack(">f", s[11:15])[0]
            e, d = (int.from_bytes(s[k:k + 2], "big") for k in (15, 17))
            e, d = (-(x & 0x7FFF) if x & 0x8000 else x for x in (e, d))
            out["scale"] = (ref, 2.0 ** e, 10.0 ** d)
        elif sec == 7:
            out["png"] = s[5:]
        i += n
    return out


def grid(msg: dict) -> np.ndarray:
    """The whole grid's stored values (uint16, north at the top): decoded once, then cropped as needed."""
    from PIL import Image

    with Image.open(io.BytesIO(msg["png"])) as img:
        return np.asarray(img)


def crop(msg: dict, south: float | None = None, north: float | None = None, west: float | None = None,
         east: float | None = None, raw: np.ndarray | None = None, block: int = 1) -> dict:
    """The box's reflectivity (dBZ, float32, north at the top; no echo or no coverage: 0) and where it is; no box:
    the whole grid. block > 1: each block×block square at its strongest (the stored values rise with dBZ)."""
    raw = grid(msg) if raw is None else raw
    r0 = 0 if north is None else max(0, int((msg["la1"] - north) / msg["dj"]))
    r1 = msg["nj"] if south is None else min(msg["nj"], int((msg["la1"] - south) / msg["dj"]) + 1)
    c0 = 0 if west is None else max(0, int((west - msg["lo1"]) / msg["di"]))
    c1 = msg["ni"] if east is None else min(msg["ni"], int((east - msg["lo1"]) / msg["di"]) + 1)
    box = raw[r0:r1, c0:c1]
    if block > 1:
        h, w = box.shape[0] // block * block, box.shape[1] // block * block
        box = box[:h, :w].reshape(h // block, block, w // block, block).max(axis=(1, 3))
    ref, b, d = msg["scale"]
    dbz = (box.astype(np.float32) * np.float32(b) + np.float32(ref)) / np.float32(d)
    dbz[dbz < 0] = 0
    return {"t": msg["t"], "north": msg["la1"] - r0 * msg["dj"], "west": msg["lo1"] + c0 * msg["di"],
            "di": msg["di"] * block, "dj": msg["dj"] * block, "dbz": dbz}


def clean(dbz: np.ndarray, speck_px: int = SPECK_PX) -> np.ndarray:
    """Weak echo and specks (connected echo of fewer than speck_px pixels) removed: what the alerts read, and
    (smoothed) what the map draws. In place."""
    from scipy import ndimage

    dbz[dbz < MIN_DBZ] = 0
    labels, n = ndimage.label(dbz > 0)
    if n:
        small = np.bincount(labels.ravel()) < speck_px
        small[0] = False
        dbz[small[labels]] = 0
    return dbz


def bands(field: dict, z: np.ndarray, smooth: float = SMOOTH, thin: int = THIN) -> list[dict]:
    """Filled polygons per BAND_STEP dBZ, as GeoJSON features ({"dbz": the band's floor})."""
    import contourpy
    from scipy import ndimage

    smooth = ndimage.gaussian_filter(z, smooth)[::thin, ::thin][::-1]   # south at row 0, for ascending y
    ny, nx = smooth.shape
    di, dj = field["di"] * thin, field["dj"] * thin
    xs = field["west"] + np.arange(nx) * di
    ys = field["north"] - (ny - 1 - np.arange(ny)) * dj
    gen = contourpy.contour_generator(xs, ys, smooth, fill_type=contourpy.FillType.OuterOffset)
    out = []
    for lo in range(MIN_DBZ, BAND_TOP + 1, BAND_STEP):
        points, offsets = gen.filled(lo, lo + BAND_STEP if lo < BAND_TOP else 1000)
        polys = []
        for pts, offs in zip(points, offsets):
            rings = [np.round(pts[a:b], 3).tolist() for a, b in zip(offs[:-1], offs[1:])
                     if b - a >= 4 and _area(pts[a:b]) >= MIN_RING]
            if rings and _area(pts[offs[0]:offs[1]]) >= MIN_RING:      # the outer ring first, then its holes
                polys.append(rings)
        if polys:
            out.append({"type": "Feature", "properties": {"dbz": lo},
                        "geometry": {"type": "MultiPolygon", "coordinates": polys}})
    return out


def _area(ring: np.ndarray) -> float:
    x, y = ring[:, 0], ring[:, 1]
    return abs(float(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))) / 2


def nearest_km(field: dict, z: np.ndarray, lat: float, lon: float, dbz: float = RAIN_DBZ) -> float | None:
    """How far the nearest echo of at least `dbz` is from (lat, lon), in km; None if there's none in the box."""
    rows, cols = np.nonzero(z >= dbz)
    if not rows.size:
        return None
    dy = (field["north"] - rows * field["dj"] - lat) * KM_PER_DEG
    dx = (field["west"] + cols * field["di"] - lon) * KM_PER_DEG * math.cos(math.radians(lat))
    return float(np.sqrt(dx * dx + dy * dy).min())


# ---- storm cells -----------------------------------------------------------------------------------------------
def cells_from(geojson: dict) -> list[dict]:
    out = []
    for f in geojson.get("features") or []:
        p, (lon, lat) = f["properties"], f["geometry"]["coordinates"]
        try:
            valid = int(datetime.fromisoformat(p["valid"].replace("Z", "+00:00")).timestamp())
        except (AttributeError, TypeError, ValueError):
            valid = None
        out.append({"id": f"{p['nexrad']}-{p['storm_id']}", "lat": lat, "lon": lon, "valid": valid,
                    "toward": (p["drct"] + 180) % 360 if p.get("drct") is not None else None,
                    "kmh": (p.get("sknt") or 0) * 1.852, "dbz": p.get("max_dbz"), "top_kft": p.get("top"),
                    "posh": p.get("posh") or 0, "poh": p.get("poh") or 0, "tvs": (p.get("tvs") or "NONE") != "NONE"})
    return out


def merge(cells: list[dict], now: float) -> list[dict]:
    """One cell per storm: where radars overlap, each reports it under its own id. The newest scan of a group
    within SAME_CELL_KM (once each is carried to `now`) is kept, with the group's highest dBZ and hail odds."""
    out = []
    for c in sorted(cells, key=lambda c: -(c["valid"] or 0)):
        here = where_at(c, now)
        same = next((k for k in out if math.hypot(*_km(*k["_at"], *here)) <= SAME_CELL_KM), None)
        if same:
            same.update(dbz=max(same["dbz"] or 0, c["dbz"] or 0), posh=max(same["posh"], c["posh"]),
                        poh=max(same["poh"], c["poh"]), tvs=same["tvs"] or c["tvs"])
        else:
            out.append({**c, "_at": here})
    return [{k: v for k, v in c.items() if k != "_at"} for c in out]


def _km(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """(east, north) km from (lat0, lon0): flat, which is close enough within a few hundred km."""
    return (lon - lon0) * KM_PER_DEG * math.cos(math.radians(lat0)), (lat - lat0) * KM_PER_DEG


def where_at(cell: dict, t: float) -> tuple[float, float]:
    """The cell's (lat, lon) at time t, carried along its motion from its scan time."""
    h = (t - (cell["valid"] or t)) / 3600
    a = math.radians(cell["toward"] or 0)
    km_e, km_n = cell["kmh"] * h * math.sin(a), cell["kmh"] * h * math.cos(a)
    return cell["lat"] + km_n / KM_PER_DEG, cell["lon"] + km_e / (KM_PER_DEG * math.cos(math.radians(cell["lat"])))


def arrival(cell: dict, lat: float, lon: float, now: float, radius_km: float, within_s: float) -> float | None:
    """Seconds from now until the cell comes within radius_km of (lat, lon) on its present course: 0 if it's
    already there, None if it won't within within_s."""
    clat, clon = where_at(cell, now)
    x, y = _km(lat, lon, clat, clon)
    if math.hypot(x, y) <= radius_km:
        return 0.0
    if cell["toward"] is None or cell["kmh"] <= 0:
        return None
    a = math.radians(cell["toward"])
    vx, vy = cell["kmh"] * math.sin(a) / 3600, cell["kmh"] * math.cos(a) / 3600       # km/s
    # |p + v t| = R: v·v t² + 2 p·v t + p·p − R² = 0, the earlier root
    qa, qb, qc = vx * vx + vy * vy, 2 * (x * vx + y * vy), x * x + y * y - radius_km ** 2
    disc = qb * qb - 4 * qa * qc
    if disc < 0:
        return None
    t = (-qb - math.sqrt(disc)) / (2 * qa)
    return t if 0 <= t <= within_s else None


# ---- the service ----------------------------------------------------------------------------------------------
class RadarService:
    def __init__(self, cfg: dict, db_path: str, get=requests.get, clock=time.time):
        self.cfg, self.db_path, self._get, self._clock = cfg, db_path, get, clock
        self.lock = threading.Lock()
        self.frame: dict | None = None          # {t, box, bands}
        self.field: dict | None = None          # the cleaned box (for the alerts)
        self.cells: list[dict] = []             # nationwide storm cells
        self.cells_at: float | None = None
        self.rain: dict[int, deque] = {}        # device -> (frame time, km to the nearest rain, or None)
        self.error: str | None = None
        self.checked_at: float | None = None
        self._modified: str | None = None
        self._next = {"frame": 0.0, "cells": 0.0}
        self._thread: threading.Thread | None = None

    def stations(self, store) -> list[dict]:
        rows = store.db.execute(
            "SELECT d.device_id, s.name, s.lat, s.lon FROM wx_devices d JOIN wx_stations s ON s.station_id = d.station_id "
            "WHERE d.device_type = 'ST' AND s.lat IS NOT NULL AND s.lon IS NOT NULL ORDER BY s.name").fetchall()
        return [{"device_id": r[0], "name": r[1], "lat": r[2], "lon": r[3]} for r in rows]

    # ---- fetching ---------------------------------------------------------------------------------------------
    def refresh_frame(self, stations: list[dict]) -> bool:
        """The newest MRMS frame, if it changed. Returns whether there was a new one."""
        headers = {"If-Modified-Since": self._modified} if self._modified else {}
        with activity.log.call("radar", "MRMS reflectivity") as a:
            r = self._get(MRMS_URL, headers=headers, timeout=60)
            if r.status_code == 304:
                a["status"], a["detail"] = "304", "no new frame"
                return False
            r.raise_for_status()
            a["detail"] = f"{len(r.content) / 1e6:.1f} MB"
        msg = decode(gzip.decompress(r.content))
        raw = grid(msg)
        us = crop(msg, raw=raw, block=US_BLOCK)
        frame = {"t": msg["t"], "box": (msg["la1"] - msg["nj"] * msg["dj"], msg["la1"], msg["lo1"], msg["lo1"] + msg["ni"] * msg["di"]),
                 "bands": bands(us, clean(us.pop("dbz"), US_SPECK), US_SMOOTH, US_THIN)}
        del us
        lats, lons = [s["lat"] for s in stations], [s["lon"] for s in stations]
        field = crop(msg, min(lats) - MARGIN, max(lats) + MARGIN, min(lons) - MARGIN, max(lons) + MARGIN, raw=raw)
        del raw
        z = clean(field.pop("dbz"))
        with self.lock:
            self.field, self.frame = {**field, "z": z}, frame
            for s in stations:
                self.rain.setdefault(s["device_id"], deque(maxlen=RAIN_HISTORY)).append(
                    (field["t"], nearest_km(field, z, s["lat"], s["lon"])))
        self._modified = r.headers.get("Last-Modified")
        return True

    def refresh_cells(self, _stations: list[dict]) -> None:
        with activity.log.call("radar", "storm cells (IEM)") as a:
            r = self._get(CELLS_URL, timeout=60)
            r.raise_for_status()
            cells = cells_from(r.json())
            merged = merge(cells, self._clock())
            a["detail"] = f"{len(merged)} of {len(cells)} cells nationwide"
        with self.lock:
            self.cells, self.cells_at = merged, self._clock()

    def tick(self, store) -> None:
        """Whatever's due. Errors are kept for the Status page; the next tick tries again."""
        stations = self.stations(store)
        if not stations:
            return
        now = self._clock()
        self.checked_at = now
        for kind, every, fetch in (("frame", FRAME_EVERY, self.refresh_frame), ("cells", CELLS_EVERY, self.refresh_cells)):
            if now < self._next[kind]:
                continue
            self._next[kind] = now + every
            try:
                fetch(stations)
                self.error = None
            except Exception as e:
                caught(e, f"radar {kind}")
                self.error = f"{type(e).__name__}: {e}"[:200]

    def start(self, stop: threading.Event, store_for) -> None:
        if self._thread:
            return

        def run():
            while not stop.is_set():
                self.tick(store_for())
                stop.wait(TICK)
        self._thread = threading.Thread(target=run, name="radar", daemon=True)
        self._thread.start()

    # ---- reading ----------------------------------------------------------------------------------------------
    def closing(self, device_id: int, now: float | None = None) -> dict | None:
        """The nearest rain to a station and how fast it's coming: {km, kmh} (kmh > 0: closing in), from the
        frames of the last RAIN_WINDOW; None without at least three frames with rain in them."""
        now = now or self._clock()
        with self.lock:
            pts = [(t, d) for t, d in self.rain.get(device_id, ()) if d is not None and now - t <= RAIN_WINDOW]
        if len(pts) < 3 or pts[-1][0] - pts[0][0] < 300:
            return None
        ts, ds = np.array([p[0] for p in pts], dtype=float), np.array([p[1] for p in pts])
        slope = np.polyfit(ts - ts[0], ds, 1)[0]                       # km per second
        return {"km": float(ds[-1]), "kmh": float(-slope * 3600), "t": int(ts[-1])}

    def snapshot(self) -> dict:
        with self.lock:
            return {"frame": self.frame, "cells": list(self.cells), "cells_at": self.cells_at, "error": self.error}
