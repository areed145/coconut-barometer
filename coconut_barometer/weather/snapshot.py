"""Radar snapshots (#77): a still PNG of the newest radar around the stations, for Home's Radar card. Built once
here, with a box/size/time API, for alert images (#73) and radar tiles (#70) to use too.

Layers, bottom to top, in Web Mercator at a whole zoom (so CARTO's tiles fit as they are):
  basemap   CARTO's dark or light tiles, fetched once and kept on disk beside the database (they never change);
            without a CARTO key (or when they can't be fetched), a plain background with a lat/lon grid
  radar     the MRMS field RadarService keeps around the stations (~1 km, cleaned), each pixel in the color of its
            level, exactly as the map draws it (radar_layer.js levelColor), at the map's default opacity
  storms    the NWS radars' cells: a dot in its level's color and a dashed track 30 and 60 minutes on
  stations  small pins
  overlay   the frame's time, and the attribution

Snapshots renders each (frame, theme, scale, box) once and keeps the last few: the card asks again every couple of
minutes, and every viewer gets the same picture."""
from __future__ import annotations

import hashlib
import io
import math
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime

import numpy as np
import requests

from .radar import MIN_DBZ, where_at

SIZE = (640, 360)               # the card's image, in CSS pixels (scale 2 for retina)
TILE = 256
ZOOMS = (4, 8)                  # the box's zoom: as close as fits the stations (and MARGIN), within these
MARGIN = 0.75                   # degrees around the stations
STALE = 15 * 60                 # a frame older than this is "Radar unavailable"
ALPHA = 0.85                    # the map's default radar opacity
BAND, LOWEST, HIGHEST = 2, 15, 75   # radar_layer.js: a level per 2 dBZ, 15 up to 75 and over
CACHED = 8
TILE_URL = "https://basemaps.cartocdn.com/rastertiles/{style}/{z}/{x}/{y}{r}.png"
TILE_RETRY = 600                # after a failed tile fetch, plain backgrounds for this long
ATTRIBUTION = "NOAA MRMS · © CARTO © OpenStreetMap"
THEMES = {   # app.css's tokens: --bg (the plain background), a grid line, --fg (text, outlines), the scrim behind text
    "dark": {"style": "dark_all", "bg": (20, 18, 16), "grid": (54, 49, 45), "fg": (243, 241, 238), "scrim": (20, 18, 16, 190),
             "pin": (54, 232, 255)},
    "light": {"style": "light_all", "bg": (239, 237, 233), "grid": (214, 210, 204), "fg": (28, 25, 23), "scrim": (255, 255, 255, 200),
              "pin": (0, 99, 220)},
}


# ---- colors: radar_layer.js's radarColor / levelColor, the same stops ------------------------------------------
def _hex(c: str) -> tuple[int, int, int]:
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def radar_color(stops: list[dict], dbz: float) -> tuple[int, int, int]:
    """The color at `dbz`, blended between the stops either side; below the first or above the last, theirs."""
    s = sorted(stops, key=lambda x: x["dbz"])
    if dbz <= s[0]["dbz"]:
        return _hex(s[0]["color"])
    j = next((i for i, x in enumerate(s) if x["dbz"] >= dbz), None)
    if j is None:
        return _hex(s[-1]["color"])
    a, b = s[j - 1], s[j]
    if b["dbz"] == a["dbz"] or b["dbz"] == dbz:
        return _hex(b["color"])
    k = (dbz - a["dbz"]) / (b["dbz"] - a["dbz"])
    return tuple(math.floor(x + (y - x) * k + 0.5) for x, y in zip(_hex(a["color"]), _hex(b["color"])))   # Math.round


def level_color(stops: list[dict], dbz: float | None) -> tuple[int, int, int] | None:
    """What a level is drawn in (the color at its floor), and a storm cell of that strength; None: no dBZ known."""
    return None if dbz is None else radar_color(stops, LOWEST + max(0, math.floor((dbz - LOWEST) / BAND)) * BAND)


# ---- the box ----------------------------------------------------------------------------------------------------
def _mx(lon: float) -> float:
    return (lon + 180) / 360


def _my(lat: float) -> float:
    s = math.sin(math.radians(max(-85.05, min(85.05, lat))))
    return 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)


class Box:
    """What a snapshot shows: centered on (lat, lon) at a whole zoom, size CSS pixels, drawn at `scale`."""

    def __init__(self, lat: float, lon: float, zoom: int, size: tuple[int, int] = SIZE, scale: int = 1):
        self.lat, self.lon, self.zoom, self.size, self.scale = lat, lon, zoom, size, scale
        self.world = TILE * scale * 2 ** zoom                       # the world's width in image pixels
        self.w, self.h = size[0] * scale, size[1] * scale
        self.x0, self.y0 = _mx(lon) * self.world - self.w / 2, _my(lat) * self.world - self.h / 2

    @classmethod
    def around(cls, points: list[tuple[float, float]], size: tuple[int, int] = SIZE, scale: int = 1,
               margin: float = MARGIN, zooms: tuple[int, int] = ZOOMS) -> "Box":
        """The closest whole zoom (within `zooms`) that fits the points and `margin` degrees around them."""
        lats, lons = [p[0] for p in points], [p[1] for p in points]
        s, n, w, e = min(lats) - margin, max(lats) + margin, min(lons) - margin, max(lons) + margin
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - (_my(s) + _my(n))))))     # the middle, in Mercator
        zoom = zooms[0]
        for z in range(zooms[1], zooms[0] - 1, -1):
            world = TILE * 2 ** z
            if (_mx(e) - _mx(w)) * world <= size[0] and (_my(s) - _my(n)) * world <= size[1]:
                zoom = z
                break
        return cls(lat, (w + e) / 2, zoom, size, scale)

    def key(self) -> tuple:
        return round(self.lat, 4), round(self.lon, 4), self.zoom, self.size, self.scale

    def px(self, lat: float, lon: float) -> tuple[float, float]:
        return _mx(lon) * self.world - self.x0, _my(lat) * self.world - self.y0

    def lonlat(self) -> tuple[np.ndarray, np.ndarray]:
        """Each column's longitude and each row's latitude (at the pixel centers)."""
        xs = (self.x0 + np.arange(self.w) + 0.5) / self.world
        ys = (self.y0 + np.arange(self.h) + 0.5) / self.world
        return xs * 360 - 180, np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * ys))))


# ---- layers -----------------------------------------------------------------------------------------------------
def radar_rgba(field: dict | None, box: Box, stops: list[dict], alpha: float = ALPHA) -> np.ndarray:
    """The radar layer, h×w×4 uint8: each pixel the color of its level (bilinear between the ~1 km cells), clear
    below MIN_DBZ and outside the field."""
    from scipy import ndimage

    out = np.zeros((box.h, box.w, 4), np.uint8)
    if not field:
        return out
    lons, lats = box.lonlat()
    rows = (field["north"] - lats) / field["dj"]
    cols = (lons - field["west"]) / field["di"]
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    dbz = ndimage.map_coordinates(field["z"], [rr, cc], order=1, cval=0.0, mode="constant")
    on = dbz >= MIN_DBZ
    levels = np.clip(LOWEST + np.floor((dbz - LOWEST) / BAND) * BAND, LOWEST, HIGHEST)
    lut = np.array([level_color(stops, d) for d in range(LOWEST, HIGHEST + 1, BAND)], np.uint8)
    out[..., :3] = lut[((levels - LOWEST) // BAND).astype(int)]
    out[..., 3] = np.where(on, round(alpha * 255), 0)
    return out


class Basemap:
    """CARTO's raster tiles, fetched once and kept in `folder` (they never change)."""

    def __init__(self, folder: str | None, get=requests.get, clock=time.time):
        self.folder, self._get, self._clock = folder, get, clock
        self.failed_at: float | None = None
        self.error: str | None = None

    def tile(self, style: str, z: int, x: int, y: int, scale: int, key: str):
        from PIL import Image

        x %= 2 ** z
        r = "@2x" if scale > 1 else ""
        path = os.path.join(self.folder, style, str(z), str(x), f"{y}{r}.png") if self.folder else None
        if path and os.path.exists(path):
            with Image.open(path) as img:
                return img.convert("RGB")
        resp = self._get(TILE_URL.format(style=style, z=z, x=x, y=y, r=r), params={"key": key}, timeout=10)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            img.save(path + ".tmp", format="PNG")
            os.replace(path + ".tmp", path)
        return img

    def draw(self, box: Box, theme: str, key: str | None):
        """The box's basemap, or None (no key, or tiles failed lately): the caller draws a plain one."""
        from PIL import Image

        if not key or (self.failed_at and self._clock() - self.failed_at < TILE_RETRY):
            return None
        side = TILE * box.scale
        img = Image.new("RGB", (box.w, box.h))
        n = 2 ** box.zoom
        try:
            for ty in range(max(0, int(box.y0 // side)), min(n - 1, int((box.y0 + box.h) // side)) + 1):
                for tx in range(int(box.x0 // side), int((box.x0 + box.w) // side) + 1):
                    img.paste(self.tile(THEMES[theme]["style"], box.zoom, tx, ty, box.scale, key),
                              (round(tx * side - box.x0), round(ty * side - box.y0)))
        except Exception as e:                      # (network, a bad key, a bad image): plain, and try later
            self.failed_at, self.error = self._clock(), f"{type(e).__name__}: {e}"[:200]
            return None
        self.failed_at = self.error = None
        return img


def plain(box: Box, theme: str):
    """No tiles: the page's background and a grid every degree (or 5, zoomed out)."""
    from PIL import Image, ImageDraw

    t = THEMES[theme]
    img = Image.new("RGB", (box.w, box.h), t["bg"])
    d = ImageDraw.Draw(img)
    lons, lats = box.lonlat()
    step = 1 if box.zoom >= 6 else 5
    for lon in range(math.ceil(lons[0] / step) * step, math.floor(lons[-1] / step) * step + 1, step):
        x = box.px(0, lon)[0]
        d.line([(x, 0), (x, box.h)], fill=t["grid"], width=box.scale)
    for lat in range(math.ceil(lats[-1] / step) * step, math.floor(lats[0] / step) * step + 1, step):
        y = box.px(lat, 0)[1]
        d.line([(0, y), (box.w, y)], fill=t["grid"], width=box.scale)
    return img


def _font(size: int):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:                               # Pillow < 10.1: the fixed bitmap font
        return ImageFont.load_default()


def _dashed(d, pts: list[tuple[float, float]], fill, width: int, dash: float, gap: float) -> None:
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        at = 0.0
        while at < length:
            a, b = at / length, min(length, at + dash) / length
            d.line([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a), (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)], fill=fill, width=width)
            at += dash + gap


def _label(d, xy: tuple[float, float], text: str, font, theme: dict, scale: int, anchor: str) -> None:
    """Text on a rounded scrim, `anchor` its corner at xy (PIL anchors: la, ra, ld, rd...)."""
    l, t, r, b = d.textbbox(xy, text, font=font, anchor=anchor)
    pad = 4 * scale
    d.rounded_rectangle((l - pad, t - pad, r + pad, b + pad), radius=4 * scale, fill=theme["scrim"])
    d.text(xy, text, font=font, fill=theme["fg"], anchor=anchor)


def render(box: Box, theme: str, stops: list[dict], field: dict | None, cells: list[dict], stations: list[dict],
           when: str | None, basemap=None) -> bytes:
    """One snapshot as PNG bytes. field None: "Radar unavailable" over the basemap. `when`: the time label."""
    from PIL import Image, ImageDraw

    t, s = THEMES[theme], box.scale
    img = (basemap or plain(box, theme)).convert("RGBA")
    if field:
        img.alpha_composite(Image.fromarray(radar_rgba(field, box, stops), "RGBA"))
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    a = round(ALPHA * 255)
    for c in cells if field else []:
        color = level_color(stops, c.get("dbz"))
        color = (*color, a) if color else (*t["grid"], 255)
        path = [box.px(*p) for p in c["path"]]
        if c.get("kmh"):
            _dashed(d, path, color, 2 * s, 4 * s, 6 * s)
            for x, y in path[1:]:
                d.ellipse((x - 2 * s, y - 2 * s, x + 2 * s, y + 2 * s), fill=color)
        (x, y), r = path[0], (8 if c.get("tvs") or (c.get("posh") or 0) >= 30 else 6) * s
        d.ellipse((x - r, y - r, x + r, y + r), fill=color, outline=t["fg"], width=(3 if c.get("tvs") else 1) * s)
    for st in stations:
        x, y = box.px(st["lat"], st["lon"])
        r = 5 * s
        d.ellipse((x - r, y - r, x + r, y + r), fill=t["pin"], outline=t["fg"], width=2 * s)
    small = _font(11 * s)
    if field and when:
        _label(d, (box.w - 8 * s, 8 * s), when, _font(14 * s), t, s, "ra")
    _label(d, (box.w - 6 * s, box.h - 6 * s), ATTRIBUTION if basemap else ATTRIBUTION.split(" · ")[0], small, t, s, "rd")
    if not field:
        _label(d, (box.w / 2, box.h / 2), "Radar unavailable", _font(18 * s), t, s, "mm")
    img.alpha_composite(over)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=False, compress_level=6)
    return out.getvalue()


def storm_cells(cells: list[dict], box: Box, at: float) -> list[dict]:
    """The cells in (or near) the box, each with where it is at `at` and 30 and 60 minutes on (as the map's)."""
    lons, lats = box.lonlat()
    pad = 0.5
    out = []
    for c in cells:
        if lons[0] - pad <= c["lon"] <= lons[-1] + pad and lats[-1] - pad <= c["lat"] <= lats[0] + pad:
            out.append({**c, "path": [where_at(c, at + m * 60) for m in (0, 30, 60)]})
    return out


def clock_label(t: int, tz: str | None) -> str:
    """'radar 12:04' in the stations' time zone (the server's, without one)."""
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(tz) if tz else None
    except Exception:
        zone = None
    dt = datetime.fromtimestamp(t, zone)
    return f"radar {dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


# ---- the cache --------------------------------------------------------------------------------------------------
class Snapshots:
    """Rendered snapshots by (frame time, theme, box), the last CACHED of them. One per app (Runtime.snapshots)."""

    def __init__(self, radar, db_path: str | None, get=requests.get, clock=time.time):
        self.radar, self._clock = radar, clock
        folder = os.path.join(os.path.dirname(os.path.abspath(db_path)), "map_tiles") if db_path else None
        self.basemap = Basemap(folder, get=get, clock=clock)
        self.lock = threading.Lock()
        self._cache: OrderedDict = OrderedDict()
        self.renders = 0

    def get(self, stations: list[dict], theme: str, scale: int, stops: list[dict], carto_key: str | None,
            tz: str | None = None) -> tuple[str, bytes]:
        """(ETag, PNG) of the newest radar around `stations`; rendered once per frame (or once while there's none)."""
        box = Box.around([(x["lat"], x["lon"]) for x in stations] or [(39.0, -96.0)], scale=scale,
                         zooms=ZOOMS if stations else (4, 4))
        with self.radar.lock:
            field, cells = self.radar.field, list(self.radar.cells)
        now = self._clock()
        if field and now - field["t"] > STALE:
            field = None
        t = field["t"] if field else None
        key = (t, theme, box.key(), tuple((x["dbz"], x["color"]) for x in stops), bool(carto_key))
        with self.lock:                             # one render at a time: two requests for a new frame render it once
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            png = render(box, theme, stops, field, storm_cells(cells, box, t) if field else [], stations,
                         clock_label(t, tz) if t else None, self.basemap.draw(box, theme, carto_key))
            self.renders += 1
            etag = '"' + hashlib.sha256(repr(key).encode()).hexdigest()[:20] + '"'
            self._cache[key] = (etag, png)
            while len(self._cache) > CACHED:
                self._cache.popitem(last=False)
            return etag, png
