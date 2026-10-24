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
view() counts the flashes in a box per minute and per cell, the cells coarser zoomed out. One per process (`glm`)."""
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


glm = Lightning()
