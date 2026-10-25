"""Lightning for the Weather map (#94): where it struck, from GOES-East's Geostationary Lightning Mapper (GLM). The
stations' own lightning is only a distance (a Tempest can't tell which way), so this is the map's located lightning.

GLM sees total lightning (in-cloud and cloud-to-ground), day and night, over the Americas and the Atlantic, and NOAA
puts out its flashes every 20 seconds, about a minute behind (public domain). Each file is NetCDF4 (~430 KB, mostly
events and groups), so it isn't downloaded: the files are listed in NOAA's AWS Open Data bucket (s3://noaa-goes19,
GLM-L2-LCFA/year/day/hour/), and only each flash's latitude and longitude are asked for from Unidata's THREDDS mirror
of the same files, over OPeNDAP as text (~15 KB a file). A flash is placed at its file's start time (20 s is plenty
for a map) and kept for KEEP, long enough for the loop with the satellite on (its frames run up to ~2½ hours back).

Fetched while the map asks: each round lists what's new since the last and fetches only the files not yet held,
newest first, a few at a time, published as they come (a first fill after the map has been closed takes a few
seconds). A file THREDDS doesn't have yet is tried again the next round. A Feed (feed.py), in the background.
view() counts the flashes in a box per minute and per cell, the cells coarser zoomed out (the map's newest flashes,
and its dots and blobs); shapes() traces them as smooth shapes, as the radar's are (#101, below). One per process
(`glm`)."""
from __future__ import annotations

import calendar
import re
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from ..gazetteer import USER_AGENT
from .feed import Feed

BUCKET = "https://noaa-goes19.s3.amazonaws.com/"                 # GOES-19 is GOES-East
PRODUCT = "GLM-L2-LCFA"
DODS = "https://thredds.ucar.edu/thredds/dodsC/satellite/goes/east/grb/GLM/LCFA/"
TTL = 60                 # GLM puts out a file every 20 seconds
KEEP = 150 * 60          # the loop with the satellite reaches ~2½ hours back
WORKERS = 6              # files fetched from THREDDS at a time
FIRST_FILL = 60          # files published at a time while catching up (the newest first)
Q = 100                  # flashes are held in whole 1/Q degrees (int16 fits ±180°)
LEVELS = {"fine": 40, "coarse": 10}       # name -> cells of 1/q degrees: ~3 km zoomed in, ~11 km out
FINE_ZOOM = 6            # radar.py's: from here in, the fine cells
_START = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})\d_")


def start_time(name: str) -> int:
    """'OR_GLM-L2-LCFA_G19_s20262972318000_e…' -> its start, epoch seconds (year, day of year, h, m, s; tenths dropped)."""
    m = _START.search(name)
    if not m:
        raise ValueError(f"not a GLM file name: {name!r}")
    year, day, h, mi, s = (int(x) for x in m.groups())
    return calendar.timegm((year, 1, 1, h, mi, s, 0, 0, 0)) + (day - 1) * 86400


def hour_prefix(t: float) -> str:
    g = time.gmtime(t)
    return f"{PRODUCT}/{g.tm_year}/{g.tm_yday:03d}/{g.tm_hour:02d}/"


def listed(xml: str) -> list[str]:
    """An S3 ListObjectsV2 page -> the file names in it (the keys' last part)."""
    return [k.rsplit("/", 1)[-1] for k in re.findall(r"<Key>([^<]+\.nc)</Key>", xml)]


def parse_ascii(text: str) -> tuple[np.ndarray, np.ndarray]:
    """OPeNDAP's text answer for flash_lat,flash_lon -> (lat, lon) as float32 arrays."""
    arrays = {}
    for name, n, values in re.findall(r"^(flash_lat|flash_lon)\[(\d+)\]\n([^\n]*)", text, re.M):
        a = np.array([float(v) for v in values.split(",") if v.strip()], dtype=np.float32)
        if len(a) != int(n):
            raise ValueError(f"{name}: {len(a)} values, expected {n}")
        arrays[name] = a
    if "flash_lat" not in arrays or "flash_lon" not in arrays or len(arrays["flash_lat"]) != len(arrays["flash_lon"]):
        raise ValueError("no flash_lat and flash_lon in the answer")
    return arrays["flash_lat"], arrays["flash_lon"]


class Lightning(Feed):
    source, what, via, ttl = "imagery", "lightning", "NOAA GOES-East GLM (AWS, Unidata THREDDS)", TTL

    def __init__(self, *a, **k):
        k.setdefault("background", True)
        super().__init__(*a, **k)
        self._files: dict[str, tuple[int, np.ndarray, np.ndarray]] = {}    # name -> (start, lat, lon in 1/Q°)
        self._last: dict[str, str] = {}                                     # hour prefix -> newest name listed
        self._missing: set[str] = set()                                     # listed, not on THREDDS yet (or failed)
        self._shapes: dict[tuple, str] = {}                                 # shapes(): kept frames' texts

    # ---- fetching -----------------------------------------------------------------------------------------------
    def _list(self, now: float) -> list[str]:
        """Every file listed in the hours KEEP covers, past what earlier rounds listed in each."""
        names, seen, last = [], set(), dict(self._last)
        for t in range(int(now - KEEP), int(now) + 3600, 3600):
            prefix = hour_prefix(min(t, now))
            if prefix in seen:
                continue
            seen.add(prefix)
            params = {"list-type": "2", "prefix": prefix}
            if prefix in last:
                params["start-after"] = prefix + last[prefix]
            r = self._get(BUCKET, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
            r.raise_for_status()
            page = listed(r.text)
            if page:
                last[prefix] = max(page)
            names += page
        self._last = {p: n for p, n in last.items() if p in seen}     # only once every hour has listed
        return names

    def _fetch(self, name: str) -> tuple[int, np.ndarray, np.ndarray] | str:
        """One file's flashes, or why not: "late" (THREDDS doesn't have it yet) or the error. Either way it's tried
        again next round: one bad file mustn't lose the round."""
        try:
            t = start_time(name)
            day = time.strftime("%Y%m%d", time.gmtime(t))
            r = self._get(f"{DODS}{day}/{name}.ascii?flash_lat,flash_lon", headers={"User-Agent": USER_AGENT}, timeout=20)
            if r.status_code == 404:
                return "late"
            r.raise_for_status()
            lat, lon = parse_ascii(r.text)
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {e}"[:200]
        return t, np.round(lat * Q).astype(np.int16), np.round(lon * Q).astype(np.int16)

    def load(self, a: dict) -> dict | None:
        now = self._clock()
        self._files = {k: v for k, v in self._files.items() if v[0] >= now - KEEP}
        wanted = set(self._list(now)) | self._missing
        todo = sorted((n for n in wanted - set(self._files) if start_time(n) >= now - KEEP), reverse=True)
        self._missing, got, failed = set(), 0, []
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for i in range(0, len(todo), FIRST_FILL):        # newest first, each batch shown once it's in
                batch = todo[i:i + FIRST_FILL]
                for name, flashes in zip(batch, pool.map(self._fetch, batch)):
                    if isinstance(flashes, str):
                        self._missing.add(name)
                        if flashes != "late":
                            failed.append(flashes)
                    else:
                        self._files[name] = flashes
                        got += 1
                data = self._pack()
                with self._lock:
                    self.data = data
        if failed and not got:
            raise RuntimeError(f"none of {len(failed)} files could be read from THREDDS: {failed[0]}")
        data = self._pack()
        a["detail"] = f"{got} new files, {len(self._files)} held, {len(data['t'])} flashes" + \
                      (f", {len(self._missing)} not read yet" if self._missing else "")
        return data

    def _pack(self) -> dict:
        files = list(self._files.values())
        if not files:
            empty = np.zeros(0, dtype=np.int16)
            return {"t": np.zeros(0, dtype=np.int64), "lat": empty, "lon": empty, "newest": None}
        return {"t": np.concatenate([np.full(len(f[1]), f[0], dtype=np.int64) for f in files]),
                "lat": np.concatenate([f[1] for f in files]), "lon": np.concatenate([f[2] for f in files]),
                "newest": max(f[0] for f in files)}

    # ---- the map ------------------------------------------------------------------------------------------------
    def view(self, bbox: tuple | None, zoom: float | None, since: float | None = None) -> dict:
        """The flashes in `bbox` (west, south, east, north; None: all) since `since`, counted per minute and cell:
        {level, q, minutes: [[minute, [lat, lon, n, lat, lon, n, ...]], ...] (oldest first; a cell is the whole
        1/q degrees at its south west corner), newest (the newest file's start), checked_at, error, pending}."""
        state = self.current()
        level = "fine" if zoom is not None and zoom >= FINE_ZOOM else "coarse"
        q, data = LEVELS[level], state["data"]
        out = {"level": level, "q": q, "minutes": [], "newest": data and data["newest"],
               **{k: state[k] for k in ("checked_at", "error", "pending")}}
        if not data or not len(data["t"]):
            return out
        t, lat, lon = data["t"], data["lat"], data["lon"]
        keep = t >= (since if since is not None else self._clock() - KEEP)
        if bbox:
            w, s, e, n = (round(v * Q) for v in bbox)
            keep &= (lon >= w) & (lon <= e) & (lat >= s) & (lat <= n)
        if not keep.any():
            return out
        minute = t[keep] // 60 * 60
        cy = np.floor_divide(lat[keep].astype(np.int32) * q, Q)
        cx = np.floor_divide(lon[keep].astype(np.int32) * q, Q)
        cells, counts = np.unique(np.stack([minute, cy, cx], axis=1), axis=0, return_counts=True)
        for m in np.unique(cells[:, 0]):
            rows = cells[:, 0] == m
            flat = np.column_stack([cells[rows, 1], cells[rows, 2], counts[rows]]).ravel()
            out["minutes"].append([int(m), flat.tolist()])
        return out

    # ---- smooth shapes, as radar's (#101) --------------------------------------------------------------------------
    def shapes(self, t: float, bbox: tuple, zoom: float | None) -> str:
        """shapes_text() for the minute `t` is in, the view and its level, kept (SHAPES_KEPT). A frame that reaches the
        newest file is kept only until a newer one comes in: its last minute is still filling."""
        level = "fine" if zoom is not None and zoom >= FINE_ZOOM else "coarse"
        minute = int(t) // 60 * 60
        with self._lock:
            data = self.data
            newest = data and data["newest"]
            key = (minute, level, tuple(bbox), newest if newest is not None and minute + 60 > newest else None)
            if key in self._shapes:
                return self._shapes[key]
        text = shapes_text(data, minute + 60, bbox, level)
        with self._lock:
            self._shapes[key] = text
            for k in list(self._shapes)[:-SHAPES_KEPT]:
                del self._shapes[k]
        return text


# ---- smooth shapes (#101) -----------------------------------------------------------------------------------------
# For the map's time t, the flashes of the TRAIL before it in the view (and a margin) are counted onto a grid and each
# spread over SIGMA_KM (a Gaussian), giving how much lightning there was around each point (its density) and how long
# ago, each flash's age weighted toward the newest (HALF_LIFE), so a storm still going is "now" even if it began 15
# minutes ago. Where there was lightning is where the density is at least EDGE of a lone flash's peak, so a lone strike
# still shows. That's traced (contourpy) as radar's levels are (shapes.py's format): FRESH levels of how recent, oldest
# first, each where there was lightning at least that recent, every one cut by the same edge, colored in the browser
# from Settings → Display → Lightning colors at its `age`. Both fields are smooth, so each level is traced on
# min(one, the other) rather than one masked by the other: no edge steps along the grid. Zoomed out the grid is coarser and the spread wider, as radar's coarse level is.
TRAIL = 15 * 60                         # the map's: seconds of lightning shown up to its time
GRID = {"fine": 0.01, "coarse": 0.04}   # degrees per density cell: ~1 km zoomed in, ~4 km out
SHAPE_Q = {"fine": 1000, "coarse": 200}
SIGMA_KM = {"fine": 8, "coarse": 20}    # a flash's spread: about a GLM pixel; wider zoomed out, where that's under a pixel
EDGE = 0.4                              # where there was lightning: this much of a lone flash's peak density
FRESH = 8                               # freshness levels, oldest to newest
HALF_LIFE = 60                          # an older flash counts half as much toward a place's age every minute
SHAPES_KEPT = 120


def _traced(field: np.ndarray, xs: np.ndarray, ys: np.ndarray, bbox: tuple, q: int, min_area: float) -> list[str]:
    """Where `field` >= 0, as shapes.py polys' texts touching bbox; rings smaller than a cell left out."""
    import contourpy

    from . import shapes

    gen = contourpy.contour_generator(xs, ys, field, fill_type=contourpy.FillType.OuterOffset)
    points, offsets = gen.filled(0.0, np.inf)
    polys = []
    for pts, offs in zip(points, offsets):
        rings = []
        for i, (lo, hi) in enumerate(zip(offs[:-1], offs[1:])):
            ring = pts[lo:hi]
            qi = None if hi - lo < 4 or _area(ring) < min_area else shapes.ring(ring, q)
            if qi is None:
                if i == 0:
                    break                      # no outer ring: none of its holes either
                continue
            rings.append(qi)
        if rings:
            p = shapes.poly(rings)
            if shapes.in_view(p, bbox, q):
                polys.append(p[4])
    return polys


def shapes_text(data: dict | None, t: float, bbox: tuple, level: str) -> str:
    """The lightning of the TRAIL up to `t` in `bbox` (west, south, east, north) at `level`, as JSON text:
    {t, level, q, bands: [{age, polys}] (oldest first)}."""
    from scipy import ndimage

    q, res = SHAPE_Q[level], GRID[level]
    head = f'{{"t":{int(t)},"level":"{level}","q":{q},'
    empty = head + '"bands":[]}'
    if not data or not len(data["t"]):
        return empty
    w, s, e, n = bbox
    stretch = 1 / max(0.2, np.cos(np.radians((s + n) / 2)))   # a degree of longitude is shorter
    sigma = SIGMA_KM[level] / 111.2
    sy, sx, pad = sigma / res, sigma * stretch / res, 4 * sigma * stretch
    age = t - data["t"]
    lat, lon = data["lat"] / Q, data["lon"] / Q
    keep = (age >= 0) & (age < TRAIL) & (lat >= s - pad) & (lat <= n + pad) & (lon >= w - pad) & (lon <= e + pad)
    if not keep.any():
        return empty
    nx, ny = int(np.ceil((e - w + 2 * pad) / res)) + 1, int(np.ceil((n - s + 2 * pad) / res)) + 1
    x0, y0 = w - pad, s - pad
    ix = np.clip(((lon[keep] - x0) / res).astype(np.int64), 0, nx - 1)
    iy = np.clip(((lat[keep] - y0) / res).astype(np.int64), 0, ny - 1)
    a = age[keep].astype(np.float64)
    weight = 0.5 ** (a / HALF_LIFE)
    fields = []
    for v in (np.ones_like(a), weight, weight * a):            # density; the weighted age's sums
        g = np.zeros((ny, nx), dtype=np.float32)
        np.add.at(g, (iy, ix), v)
        fields.append(ndimage.gaussian_filter(g, (sy, sx), truncate=3.0))
    density, wsum, asum = fields
    impulse = np.zeros((int(8 * sy) | 1, int(8 * sx) | 1), dtype=np.float32)
    impulse[impulse.shape[0] // 2, impulse.shape[1] // 2] = 1
    lone = float(ndimage.gaussian_filter(impulse, (sy, sx), truncate=3.0).max())
    with np.errstate(invalid="ignore", divide="ignore"):
        fresh = 1 - np.where(wsum > 1e-9, asum / wsum, TRAIL) / TRAIL   # 1 now, 0 at the trail's end
    there = density / (EDGE * lone) - 1                        # >= 0 where there was lightning
    xs, ys, min_area = x0 + np.arange(nx) * res, y0 + np.arange(ny) * res, res * res
    bands = []
    for k in range(FRESH):
        r = k / FRESH
        polys = _traced(np.minimum(there, (fresh - r) * 4) if k else there, xs, ys, bbox, q, min_area)
        if polys:
            bands.append(f'{{"age":{round((1 - r) * TRAIL)},"polys":[{",".join(polys)}]}}')
    return head + f'"bands":[{",".join(bands)}]}}'


def _area(ring: np.ndarray) -> float:
    x, y = ring[:, 0], ring[:, 1]
    return abs(float(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))) / 2


glm = Lightning()
