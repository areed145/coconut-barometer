"""Makes coconut_barometer/web/static/vendor/natural-earth-lines.json: the coastlines, lakes, country borders and
state and province lines the Weather map draws over the satellite (#95), whose imagery hides the basemap's own.

From Natural Earth's 1:50m vectors (public domain, naturalearthdata.com), on the side of the world GOES-East sees
(any line with a point west of 0°). Each line is simplified (Douglas-Peucker, TOLERANCE degrees) and kept in whole
1/Q degrees as [x0, y0, dx1, dy1, ...] (lon, lat; each point after the first as the step from the one before).

    curl -LO https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_coastline.geojson
    (and ne_50m_lakes, ne_50m_admin_0_boundary_lines_land, ne_50m_admin_1_states_provinces_lines)
    python tools/natural_earth_lines.py <folder with them>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

Q = 100
TOLERANCE = 0.02
SOURCES = {"coast": "ne_50m_coastline", "lake": "ne_50m_lakes", "country": "ne_50m_admin_0_boundary_lines_land",
           "state": "ne_50m_admin_1_states_provinces_lines"}
OUT = Path(__file__).resolve().parent.parent / "coconut_barometer" / "web" / "static" / "vendor" / "natural-earth-lines.json"


def simplify(pts: list, tol: float) -> list:
    """Douglas-Peucker, without recursion."""
    if len(pts) < 3:
        return pts
    keep, stack = {0, len(pts) - 1}, [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        norm = (dx * dx + dy * dy) ** 0.5
        far, best = -1.0, None
        for i in range(a + 1, b):
            px, py = pts[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / norm if norm else ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            if d > far:
                far, best = d, i
        if best is not None and far > tol:
            keep.add(best)
            stack += [(a, best), (best, b)]
    return [pts[i] for i in sorted(keep)]


def parts(geometry: dict) -> list:
    t, c = geometry["type"], geometry["coordinates"]
    if t == "LineString":
        return [c]
    if t in ("MultiLineString", "Polygon"):
        return c
    if t == "MultiPolygon":
        return [ring for polygon in c for ring in polygon]
    return []


def encode(line: list) -> list[int] | None:
    pts, out, last = [(round(x * Q), round(y * Q)) for x, y in simplify(line, TOLERANCE)], [], None
    for p in pts:
        if p == last:
            continue
        out += list(p) if last is None else [p[0] - last[0], p[1] - last[1]]
        last = p
    return out if len(out) >= 4 else None


def main(folder: str) -> None:
    layers = {}
    for name, source in SOURCES.items():
        features = json.loads((Path(folder) / f"{source}.geojson").read_text())["features"]
        lines = [encode(p) for f in features if f["geometry"] for p in parts(f["geometry"]) if any(x <= 0 for x, _ in p)]
        layers[name] = [x for x in lines if x]
    OUT.write_text(json.dumps({"source": "Natural Earth 1:50m (public domain), naturalearthdata.com", "q": Q, "layers": layers},
                              separators=(",", ":")))
    print(OUT, OUT.stat().st_size, {k: len(v) for k, v in layers.items()})


if __name__ == "__main__":
    main(sys.argv[1])
