"""Radar (#54): smooth reflectivity shapes for the Weather map, nationwide storm cells, and what the
rain and storm alerts need (alerts.py).

- Reflectivity: NOAA MRMS's merged, quality-controlled composite (non-weather echoes already removed, every
  radar merged), every 2 minutes on a 0.01° (~1 km) grid over the US. The `latest` file is fetched only when it
  has changed (If-Modified-Since). Its GRIB2 data is a PNG, so Pillow decodes it, once, and it's used twice:
    the map    the whole US, each US_BLOCK×US_BLOCK block kept at its strongest (~2 km, so a core isn't averaged
               away), cleaned of specks, and traced (contourpy) at two LEVELS of detail into a shape per BAND_STEP
               dBZ level and up (encode()). A request gets the fine level for the part of the US in view when zoomed
               in, the coarse one zoomed out (view()); the browser draws them on one canvas, colored from Settings →
               Display → Radar colors. Shrinking first keeps the memory down (~170 MB at the peak, not ~500).
    alerts     MARGIN degrees around the stations at the full ~1 km, cleaned (not smoothed): the distance from
               each station to the nearest rain.
- Storm cells: the NWS radars' own storm tracking, as the Iowa Environmental Mesonet publishes it: every
  cell's position, motion, max dBZ, hail probability and rotation flags, every 5 minutes. `drct` is the
  direction a cell moves *from* (checked against successive scans), so it heads `drct` + 180.

The radar loop: each frame's shapes and the storm cells then are written to a folder beside the database, and
frames older than LOOP_WINDOW are deleted, so the loop plays the same MRMS vectors and storm tracks as the live view.

Otherwise everything is held in memory: the latest frame, the latest cells, and each station's distance to the nearest
rain over the last half hour (how fast it shrinks is the rain alert's ETA). One per app (Runtime.radar)."""
from __future__ import annotations

import calendar
import gzip
import io
import json
import math
import os
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
US_BLOCK, US_SPECK = 2, 2   # the map's whole-US field: ~2 km pixels (each 2×2 block at its strongest), and specks
# The map's two levels of detail, both from that field: name -> (Gaussian sigma in its pixels, traced on every n-th
# pixel, coordinates in whole 1/q degrees). Fine is ~2 km, sent only for the part of the US in view; coarse is ~8 km
# for the whole US, zoomed out, where a screen pixel is several km anyway.
LEVELS = {"fine": (1.0, 1, 1000), "coarse": (2.0, 4, 100)}
FINE_ZOOM = 6            # the map's zoom from which the fine level is sent (a pixel is ~2 km there)
SAME_CELL_KM = 8         # cells this close are one storm seen by more than one radar
# A storm's track across refreshes (#71): a cell this close to where an earlier one should be by now continues it
# (matched by place, not id: the id changes when another radar reports it). Its motion is "steady" while each scan's
# heading and speed agree with the last one's and with how far it actually went: within HEADING_DEG, and SPEED_DIFF
# of the faster (at least SPEED_FLOOR km/h: a centroid wanders a few km between scans). Below SLOW_KMH, heading
# means little, so any is consistent.
SAME_TRACK_KM, HEADING_DEG, SPEED_DIFF, SPEED_FLOOR, SLOW_KMH = 15, 45, 0.5, 25, 20
STEADY_SCANS = 2         # scans in a row a storm's motion must hold before it can alert (#71)
BAND_STEP, BAND_TOP = 5, 75
RAIN_DBZ, HERE_KM = 20, 3.0          # rain, and close enough to be raining at the station already
RAIN_HISTORY, RAIN_WINDOW = 20, 20 * 60
KM_PER_DEG = 111.2
STORED_CACHE = 36       # loop frames held in memory once read (about an hour's loop: ~1 MB each)
LOOP_WINDOW = 135 * 60   # the radar loop's frames (and the storm cells with each) are kept on disk this long, then deleted:
                         # the map loops an hour, ending at the newest satellite frame when that's on (up to ~60 min
                         # behind: NASA GIBS publishes late), so the radar keeps that hour plus the delay (#74)


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


def encode(field: dict, z: np.ndarray, smooth: float, thin: int, q: int) -> list[list]:
    """The map's shapes at one level of detail: for each BAND_STEP dBZ level, everything at that level *or above*
    (so the bands are drawn stacked, lowest first, and neighbours can't show a seam between them: #67), as
    [dbz, [poly, ...]]. A poly is [west, south, east, north, text]: its bounds in whole 1/q degrees, and its JSON
    text `[w,s,e,n,ring,ring...]` (the outer ring first, then its holes), each ring flat `[x0,y0,dx1,dy1,...]`, every
    point after the first the step from the one before. The text is made once, here: a request picks the polys in
    view and joins their texts (view())."""
    import contourpy
    from scipy import ndimage

    grid = ndimage.gaussian_filter(z, smooth)[::thin, ::thin][::-1]     # south at row 0, for ascending y
    ny, nx = grid.shape
    di, dj = field["di"] * thin, field["dj"] * thin
    xs = field["west"] + np.arange(nx) * di
    ys = field["north"] - (ny - 1 - np.arange(ny)) * dj
    gen = contourpy.contour_generator(xs, ys, grid, fill_type=contourpy.FillType.OuterOffset)
    min_area = di * dj                     # a ring smaller than one traced cell is a speck or a pinhole
    out = []
    for lo in range(MIN_DBZ, BAND_TOP + 1, BAND_STEP):
        points, offsets = gen.filled(lo, 1000)
        polys = []
        for pts, offs in zip(points, offsets):
            rings = []
            for i, (a, b) in enumerate(zip(offs[:-1], offs[1:])):
                if b - a < 4 or _area(pts[a:b]) < min_area:
                    if i == 0:
                        break                  # no outer ring: none of its holes either
                    continue
                qi = np.round(pts[a:b] * q).astype(np.int64)
                step = np.diff(qi, axis=0)
                qi = np.vstack([qi[:1], qi[1:][np.any(step != 0, axis=1)]])   # rounding can repeat a point
                if len(qi) > 1 and (qi[0] == qi[-1]).all():
                    qi = qi[:-1]                                              # closed by the drawing, not a point
                if len(qi) < 3:
                    if i == 0:
                        break
                    continue
                if i == 0:
                    (w, s), (e, n) = qi.min(axis=0), qi.max(axis=0)
                rings.append(np.concatenate([qi[:1], np.diff(qi, axis=0)]).ravel().tolist())
            if rings:
                bounds = [int(w), int(s), int(e), int(n)]
                polys.append([*bounds, json.dumps([*bounds, *rings], separators=(",", ":"))])
        if polys:
            out.append([lo, polys])
    return out


def view(frame: dict, bbox: tuple | None = None, zoom: float | None = None) -> str:
    """A frame's shapes for the map, as JSON text: the fine level from FINE_ZOOM in (only the polys touching `bbox`,
    west/south/east/north degrees), else the coarse one (also filtered, if `bbox` is given)."""
    level = "fine" if zoom is not None and zoom >= FINE_ZOOM else "coarse"
    q = LEVELS[level][2]
    if bbox:
        w, s, e, n = (x * q for x in bbox)
    bands = []
    for dbz, polys in (frame.get("levels") or {}).get(level, []):
        texts = [p[4] for p in polys if not bbox or (p[0] <= e and p[2] >= w and p[1] <= n and p[3] >= s)]
        if texts:
            bands.append(f'{{"dbz":{dbz},"polys":[{",".join(texts)}]}}')
    return f'{{"level":"{level}","q":{q},"bands":[{",".join(bands)}]}}'


def _area(ring: np.ndarray) -> float:
    x, y = ring[:, 0], ring[:, 1]
    return abs(float(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))) / 2


def nearest(field: dict, z: np.ndarray, lat: float, lon: float, dbz: float = RAIN_DBZ) -> tuple[float, float] | None:
    """The nearest echo of at least `dbz` to (lat, lon): (km, the way to it in degrees from north); None if there's
    none in the box."""
    rows, cols = np.nonzero(z >= dbz)
    if not rows.size:
        return None
    dy = (field["north"] - rows * field["dj"] - lat) * KM_PER_DEG
    dx = (field["west"] + cols * field["di"] - lon) * KM_PER_DEG * math.cos(math.radians(lat))
    d = dx * dx + dy * dy
    i = int(np.argmin(d))
    return float(math.sqrt(d[i])), math.degrees(math.atan2(dx[i], dy[i])) % 360


def nearest_km(field: dict, z: np.ndarray, lat: float, lon: float, dbz: float = RAIN_DBZ) -> float | None:
    """How far the nearest echo of at least `dbz` is from (lat, lon), in km; None if there's none in the box."""
    found = nearest(field, z, lat, lon, dbz)
    return found and found[0]


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


def _turn(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def consistent(toward_a, kmh_a, toward_b, kmh_b) -> bool:
    """Two motions that could be the same storm's: see SAME_TRACK_KM above."""
    if max(kmh_a or 0, kmh_b or 0) < SLOW_KMH:
        return True
    if toward_a is None or toward_b is None or kmh_a is None or kmh_b is None:
        return False
    return (_turn(toward_a, toward_b) <= HEADING_DEG
            and abs(kmh_a - kmh_b) <= max(SPEED_FLOOR, SPEED_DIFF * max(kmh_a, kmh_b)))


def track(cells: list[dict], previous: list[dict], now: float) -> list[dict]:
    """Each cell (in place), matched to the storm it continues from `previous` (the last refresh's): `track` (that
    storm's first id), `steady` (scans in a row its motion held up, this one included: 1 for a new storm or one
    that jumped), `obs_kmh`/`obs_toward` (how it actually moved since). A refresh with no new scan of it keeps those."""
    for c in cells:
        cv = c["valid"] or now
        best, near = None, SAME_TRACK_KM
        for p in previous:
            d = min(math.hypot(*_km(c["lat"], c["lon"], *where_at(p, cv))), math.hypot(*_km(c["lat"], c["lon"], p["lat"], p["lon"])))
            if d <= near:
                best, near = p, d
        if best is None:
            c.update(track=c["id"], steady=1, obs_kmh=None, obs_toward=None)
            continue
        c["track"] = best.get("track", best["id"])
        hours = (cv - (best["valid"] or cv)) / 3600
        if hours <= 0:                                        # the same scan again
            c.update(steady=best.get("steady", 1), obs_kmh=best.get("obs_kmh"), obs_toward=best.get("obs_toward"))
            continue
        east, north = _km(best["lat"], best["lon"], c["lat"], c["lon"])
        obs_kmh, obs_toward = math.hypot(east, north) / hours, math.degrees(math.atan2(east, north)) % 360
        held = consistent(c["toward"], c["kmh"], best["toward"], best["kmh"]) and consistent(c["toward"], c["kmh"], obs_toward, obs_kmh)
        c.update(obs_kmh=obs_kmh, obs_toward=obs_toward, steady=best.get("steady", 1) + 1 if held else 1)
    return cells


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
        self.errors = {"frame": None, "cells": None}     # the last round's problem with each, for the Status page
        self.checked_at: float | None = None
        self._modified: str | None = None
        self._next = {"frame": 0.0, "cells": 0.0}
        self._thread: threading.Thread | None = None
        self._stored: dict[int, dict] = {}       # stored(): the frames read last, oldest first
        self.dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "radar_frames") if db_path else None

    def stations(self, store) -> list[dict]:
        rows = store.db.execute(
            "SELECT d.device_id, s.name, s.lat, s.lon FROM wx_devices d JOIN wx_stations s ON s.station_id = d.station_id "
            "WHERE d.device_type = 'ST' AND s.lat IS NOT NULL AND s.lon IS NOT NULL ORDER BY s.name").fetchall()
        return [{"device_id": r[0], "name": r[1], "lat": r[2], "lon": r[3]} for r in rows]

    # ---- fetching ---------------------------------------------------------------------------------------------
    def refresh_frame(self, stations: list[dict]) -> bool:
        """The newest MRMS frame, if it changed. Returns whether there was a new one."""
        headers = {"If-Modified-Since": self._modified} if self._modified else {}
        data, truncated = None, ""
        with activity.log.call("radar", "MRMS reflectivity") as a:
            for attempt in range(2):
                r = self._get(MRMS_URL, headers=headers, timeout=60)
                if r.status_code == 304:
                    a["status"], a["detail"] = "304", "no new frame" + truncated
                    return False
                r.raise_for_status()
                a["detail"] = f"{len(r.content) / 1e6:.1f} MB" + truncated
                try:
                    data = gzip.decompress(r.content)
                    break
                except (EOFError, OSError):
                    truncated += " (truncated download)"
                    a["detail"] = f"{len(r.content) / 1e6:.1f} MB" + truncated
        if data is None:
            self.errors["frame"] = "MRMS frame download was truncated twice; trying again next round"
            return False
        msg = decode(data)
        raw = grid(msg)
        us = crop(msg, raw=raw, block=US_BLOCK)
        z = clean(us.pop("dbz"), US_SPECK)
        frame = {"t": msg["t"], "box": (msg["la1"] - msg["nj"] * msg["dj"], msg["la1"], msg["lo1"], msg["lo1"] + msg["ni"] * msg["di"]),
                 "levels": {name: encode(us, z, *spec) for name, spec in LEVELS.items()}}
        del us, z
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
        self._save(frame)
        return True

    # ---- the loop's frames on disk ----------------------------------------------------------------------------
    def _path(self, t: int) -> str:
        return os.path.join(self.dir, f"{int(t)}.json.gz")

    def _times(self) -> list[int]:
        try:
            names = os.listdir(self.dir) if self.dir else []
        except OSError:
            return []
        return sorted(int(n[:-8]) for n in names if n.endswith(".json.gz") and n[:-8].isdigit())

    def _save(self, frame: dict) -> None:
        """Keep this frame, with the storm cells as they are now, for the loop; delete what's too old."""
        if not self.dir:
            return
        try:
            os.makedirs(self.dir, exist_ok=True)
            with self.lock:
                cells = list(self.cells)
            tmp = self._path(frame["t"]) + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                json.dump({**frame, "cells": cells}, f)
            os.replace(tmp, self._path(frame["t"]))
            newest = max(self._times() or [frame["t"]])
            for t in self._times():
                if newest - t > LOOP_WINDOW:
                    os.remove(self._path(t))
        except OSError as e:
            caught(e, "radar frame storage")

    def history(self) -> list[int]:
        """Times of the frames kept for the loop, oldest first."""
        return self._times()

    def stored(self, t: int) -> dict | None:
        """A kept frame: {t, box, levels, cells}, or None. The last STORED_CACHE read are kept in memory: a playing
        loop asks for each again whenever the map moves."""
        with self.lock:
            if t in self._stored:
                return self._stored[t]
        if not self.dir or t not in self._times():
            return None
        try:
            with gzip.open(self._path(t), "rt", encoding="utf-8") as f:
                frame = json.load(f)
        except (OSError, ValueError):
            return None
        with self.lock:
            self._stored[t] = frame
            while len(self._stored) > STORED_CACHE:
                self._stored.pop(next(iter(self._stored)))
        return frame

    def refresh_cells(self, _stations: list[dict]) -> None:
        with activity.log.call("radar", "storm cells (IEM)") as a:
            r = self._get(CELLS_URL, timeout=60)
            r.raise_for_status()
            cells = cells_from(r.json())
            now = self._clock()
            with self.lock:
                previous = self.cells
            merged = track(merge(cells, now), previous, now)
            a["detail"] = f"{len(merged)} of {len(cells)} cells nationwide"
        with self.lock:
            self.cells, self.cells_at = merged, now

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
            self.errors[kind] = None
            try:
                fetch(stations)                     # may leave its own error (a truncated frame) without raising
            except Exception as e:
                caught(e, f"radar {kind}")
                self.errors[kind] = f"{type(e).__name__}: {e}"[:200]

    @property
    def error(self) -> str | None:
        return "; ".join(e for e in self.errors.values() if e) or None

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
        frames of the last RAIN_WINDOW; None without at least three frames with rain in them. With how well a steady
        approach fits (r2) and the fastest step closer between two frames (step_kmh): a new echo forming nearer
        is a jump, not an approach (#71)."""
        now = now or self._clock()
        with self.lock:
            pts = [(t, d) for t, d in self.rain.get(device_id, ()) if d is not None and now - t <= RAIN_WINDOW]
        if len(pts) < 3 or pts[-1][0] - pts[0][0] < 300:
            return None
        ts, ds = np.array([p[0] for p in pts], dtype=float), np.array([p[1] for p in pts])
        slope, cut = np.polyfit(ts - ts[0], ds, 1)                     # km per second
        fit = slope * (ts - ts[0]) + cut
        spread = float(((ds - ds.mean()) ** 2).sum())
        r2 = 1 - float(((ds - fit) ** 2).sum()) / spread if spread else 1.0
        step = max(0.0, float(np.max(-np.diff(ds) / np.diff(ts))) * 3600)   # km/h, the fastest closing between frames
        return {"km": float(ds[-1]), "kmh": float(-slope * 3600), "t": int(ts[-1]), "r2": r2, "step_kmh": step}

    def snapshot(self) -> dict:
        with self.lock:
            return {"frame": self.frame, "cells": list(self.cells), "cells_at": self.cells_at, "error": self.error}
