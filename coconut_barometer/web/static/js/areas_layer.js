// The Photo map's places (#164): every country photos were taken in shaded, and in the US every state instead, deeper
// the more photos. Natural Earth's 1:50m areas (public domain; tools/natural_earth_areas.py makes the file), loaded the
// first time they're shown. A photo counts for the area whose shape it's inside, so the shading agrees with what's
// drawn; one just off a simplified coast (a beach, a boat) counts for the nearest area within SNAP degrees.

import { decodeLine } from "./lines_layer.js";

const SNAP = 0.1;                 // degrees, ~10 km
const CELL = 100;                 // photos are counted per 1/CELL degree: a crowd at one spot is looked up once

let loading = null;
// The areas as [{id, polys: [[ring, ...], ...], bounds}] (each ring decodeLine's {pts, bounds}), fetched once.
export function loadAreas() {
  if (!loading) loading = fetch(new URL("../vendor/natural-earth-areas.json", import.meta.url))
    .then((r) => { if (!r.ok) throw new Error(`areas: HTTP ${r.status}`); return r.json(); })
    .then(decodeAreas)
    .catch((e) => { loading = null; throw e; });
  return loading;
}

export function decodeAreas(d) {
  return d.areas.map(({ id, polys }) => {
    const p = polys.map((rings) => rings.map((enc) => decodeLine(enc, d.q)));
    const bounds = p.flat().reduce(([w, s, e, n], { bounds: b }) =>
      [Math.min(w, b[0]), Math.min(s, b[1]), Math.max(e, b[2]), Math.max(n, b[3])], [Infinity, Infinity, -Infinity, -Infinity]);
    return { id, polys: p, bounds };
  });
}

const near = ([w, s, e, n], lat, lon, pad) => lon >= w - pad && lon <= e + pad && lat >= s - pad && lat <= n + pad;

// Inside a polygon: an odd number of its rings' edges crossed going east (so not in a hole).
function inside(rings, lat, lon) {
  let odd = false;
  for (const { pts, bounds } of rings) {
    if (!near(bounds, lat, lon, 0)) continue;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const [ya, xa] = pts[i], [yb, xb] = pts[j];
      if ((ya > lat) !== (yb > lat) && lon < xa + ((lat - ya) / (yb - ya)) * (xb - xa)) odd = !odd;
    }
  }
  return odd;
}

// How far (in degrees, east-west scaled by latitude) a point is from a ring's edges.
function distance(pts, lat, lon) {
  const k = Math.cos((lat * Math.PI) / 180);
  let best = Infinity;
  for (let i = 1; i < pts.length; i++) {
    const ax = pts[i - 1][1] * k, ay = pts[i - 1][0], dx = pts[i][1] * k - ax, dy = pts[i][0] - ay;
    const t = dx || dy ? Math.max(0, Math.min(1, ((lon * k - ax) * dx + (lat - ay) * dy) / (dx * dx + dy * dy))) : 0;
    best = Math.min(best, Math.hypot(lon * k - ax - t * dx, lat - ay - t * dy));
  }
  return best;
}

// The id of the area a point is in (or just off), or null.
export function areaAt(areas, lat, lon) {
  let best = null, far = SNAP;
  for (const a of areas) {
    if (!near(a.bounds, lat, lon, SNAP)) continue;
    if (near(a.bounds, lat, lon, 0) && a.polys.some((rings) => inside(rings, lat, lon))) return a.id;
    for (const rings of a.polys) {
      for (const { pts, bounds } of rings) {
        if (!near(bounds, lat, lon, far)) continue;
        const d = distance(pts, lat, lon);
        if (d < far) { far = d; best = a.id; }
      }
    }
  }
  return best;
}

// The map's points ([id, lat, lon, ...]) counted by area: Map id -> photos.
export function countAreas(areas, points) {
  const seen = new Map(), counts = new Map();
  for (const [, lat, lon] of points) {
    const key = Math.round(lat * CELL) * 1e6 + Math.round(lon * CELL);
    if (!seen.has(key)) seen.set(key, areaAt(areas, lat, lon));
    const id = seen.get(key);
    if (id) counts.set(id, (counts.get(id) || 0) + 1);
  }
  return counts;
}

// A layer of the areas with photos, under the photos. fill(t): t in 0..1 (log of the count) -> a CSS color.
export function areasLayer(L, areas, counts, fill) {
  const most = Math.max(2, ...counts.values());
  const renderer = L.canvas({ padding: 0.3 });
  return L.layerGroup(areas.filter((a) => counts.has(a.id)).map((a) => {
    const color = fill(Math.log(counts.get(a.id)) / Math.log(most));
    return L.polygon(a.polys.map((rings) => rings.map((r) => r.pts)), {
      renderer, interactive: false, color, weight: 1, opacity: 0.7, fillColor: color, fillOpacity: 0.35,
    });
  }));
}
