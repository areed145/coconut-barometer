"""Makes coconut_barometer/web/static/vendor/natural-earth-areas.json: the countries, and the US states in place of the
US, that the Photo map shades where photos were taken (#164).

From Natural Earth's 1:50m vectors (public domain, naturalearthdata.com), the whole world. Each area is its id (the
ISO 3166 code: "FR", "US-UT"; Natural Earth's ADM0_A3 for the few without one) and its polygons, each a list of rings
(the outline, then any holes) kept the way natural_earth_lines.py keeps a line.

    curl -LO https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson
    (and ne_50m_admin_1_states_provinces)
    python tools/natural_earth_areas.py <folder with them>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from natural_earth_lines import Q, encode

OUT = Path(__file__).resolve().parent.parent / "coconut_barometer" / "web" / "static" / "vendor" / "natural-earth-areas.json"


def polygons(geometry: dict) -> list:
    """The polygons as [[ring, ...], ...], each ring encoded; rings simplified away are dropped, and a polygon
    whose outline is."""
    c = geometry["coordinates"]
    out = []
    for polygon in [c] if geometry["type"] == "Polygon" else c:
        rings = [encode(ring) for ring in polygon]
        if rings[0] and len(rings[0]) >= 6:
            out.append([r for r in rings if r and len(r) >= 6])
    return out


def main(folder: str) -> None:
    def read(name):
        return json.loads((Path(folder) / f"{name}.geojson").read_text())["features"]

    polys: dict[str, list] = {}         # id -> polygons: Australia's outlying islands are Australia's
    for f in read("ne_50m_admin_0_countries"):
        p = f["properties"]
        if p["ADM0_A3"] != "USA":
            polys.setdefault(p["ISO_A2_EH"] if p["ISO_A2_EH"] != "-99" else p["ADM0_A3"], []).extend(polygons(f["geometry"]))
    for f in read("ne_50m_admin_1_states_provinces"):
        p = f["properties"]
        if p["adm0_a3"] == "USA":
            polys.setdefault(p["iso_3166_2"], []).extend(polygons(f["geometry"]))
    areas = [{"id": k, "polys": v} for k, v in polys.items() if v]
    OUT.write_text(json.dumps({"source": "Natural Earth 1:50m (public domain), naturalearthdata.com", "q": Q, "areas": areas},
                              separators=(",", ":")))
    print(OUT, OUT.stat().st_size, len(areas))


if __name__ == "__main__":
    main(sys.argv[1])
