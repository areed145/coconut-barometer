"""Lambert conformal grids (GRIB2 template 3.30: RTMA, HRRR) onto the regular lat/lon grids the Weather map draws.

The projection is closed-form on a sphere (Snyder, Map Projections: A Working Manual, §15), so no pyproj. Resampling
goes the other way round from the grid: each lat/lon point wanted is projected into the Lambert grid and read there
(bilinear), which is simpler and quicker than placing millions of Lambert points and interpolating between them.

Winds on these grids are usually along the grid's axes (grid_relative), not east and north. The grid's "up" leans
from north by n·(lon − LoV), so they're turned by that to be earth-relative (earth_relative()). Checked against RTMA's
own WDIR: within a degree, where unturned is ~10° off at the edges of the US."""
from __future__ import annotations

import math

import numpy as np


class Lambert:
    def __init__(self, grid: dict):
        """grid: grib.message()'s template 3.30 grid."""
        if grid["template"] != 30:
            raise ValueError("not a Lambert conformal grid")
        self.grid = grid
        p1, p2 = math.radians(grid["latin1"]), math.radians(grid["latin2"])
        t = lambda p: math.tan(math.pi / 4 + p / 2)
        self.n = math.sin(p1) if abs(p1 - p2) < 1e-9 else math.log(math.cos(p1) / math.cos(p2)) / math.log(t(p2) / t(p1))
        self.rf = grid["radius"] * math.cos(p1) * t(p1) ** self.n / self.n
        self.x0, self.y0 = (float(v) for v in self.xy(grid["la1"], grid["lo1"]))

    def theta(self, lon):
        """The angle (radians) the grid's up leans from north at `lon`."""
        return self.n * np.radians((np.asarray(lon, dtype=np.float64) - self.grid["lov"] + 180) % 360 - 180)

    def xy(self, lat, lon):
        """Metres on the projection plane, the cone's apex at the origin."""
        rho = self.rf / np.tan(math.pi / 4 + np.radians(np.asarray(lat, dtype=np.float64)) / 2) ** self.n
        th = self.theta(lon)
        return rho * np.sin(th), -rho * np.cos(th)

    def ij(self, lat, lon):
        """Where (lat, lon) is in the grid: (column, row) as floats, the first point (la1, lo1) at (0, 0) and rows
        counted in the file's order."""
        x, y = self.xy(lat, lon)
        i, j = (x - self.x0) / self.grid["dx"], (y - self.y0) / self.grid["dy"]
        return i, (j if self.grid["south_first"] else -j)

    def earth_relative(self, u: np.ndarray, v: np.ndarray, lon) -> tuple[np.ndarray, np.ndarray]:
        """Grid-relative wind components at longitudes `lon` (broadcast) -> eastward and northward."""
        th = self.theta(lon)
        c, s = np.cos(th), np.sin(th)
        return (c * u + s * v).astype(np.float32), (c * v - s * u).astype(np.float32)


def latlon_grid(south: float, north: float, west: float, east: float, step: float) -> dict:
    """A regular grid covering the box, whole steps from (south, west): {lat0, lon0, dlat, dlon, nx, ny}."""
    ny, nx = int(math.floor((north - south) / step)) + 1, int(math.floor((east - west) / step)) + 1
    return {"lat0": south, "lon0": west, "dlat": step, "dlon": step, "nx": nx, "ny": ny}


def points(box: dict) -> tuple[np.ndarray, np.ndarray]:
    """A latlon_grid's (lats, lons), each (ny, nx), south at row 0."""
    lats = box["lat0"] + np.arange(box["ny"]) * box["dlat"]
    lons = box["lon0"] + np.arange(box["nx"]) * box["dlon"]
    return np.meshgrid(lats, lons, indexing="ij")


def resample(lam: Lambert, field: np.ndarray, box: dict, smooth: float = 0) -> np.ndarray:
    """`field` (the Lambert grid's values, rows in the file's order) at each point of `box` (latlon_grid()),
    bilinear, float32 (ny, nx), south at row 0; NaN outside the grid. smooth > 0: a Gaussian of that many grid
    points first, so a coarse box averages over what's between its points instead of picking one."""
    from scipy import ndimage

    if smooth:
        field = ndimage.gaussian_filter(field, smooth)
    lats, lons = points(box)
    i, j = lam.ij(lats, lons)
    return ndimage.map_coordinates(field, [j, i], order=1, mode="constant", cval=np.nan).astype(np.float32)
