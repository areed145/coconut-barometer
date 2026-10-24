// The Weather map's smooth radar, drawn on one canvas (#70). The server sends a frame's shapes compactly
// (weather/radar.py encode()): per dBZ level, everything at that level and up, each polygon its bounds and rings of
// whole 1/q degrees, every point after the first the step from the one before. decode() turns that into flat
// Float32Arrays already projected to Web Mercator (0–1 across the world), once per frame; the layer then draws
// whichever frame it's given, culling to the view, so a loop step is one canvas redraw - nothing is rebuilt and no
// Leaflet object is made per polygon. map_canvas.js keeps the canvas on screen while the map moves.

import { mapCanvas } from "./map_canvas.js";

// ---- colors (Settings → Display → Radar colors: dBZ → color stops), one place for the levels, the storm cells,
// the legend and the alert page (#75) ---------------------------------------------------------------------------
export const DEFAULT_STOPS = [{ dbz: 15, color: "#8fd3ff" }, { dbz: 25, color: "#2fa84f" }, { dbz: 35, color: "#f2d22e" },
  { dbz: 45, color: "#f28c28" }, { dbz: 55, color: "#d7301f" }, { dbz: 65, color: "#b03ab5" }];   // config.py's
export const BAND = 2, LOWEST = 15, HIGHEST = 75;      // radar.py: a level per 2 dBZ, 15 up to 75 and over
const hex = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
// The color at `dbz`, blended between the stops either side; below the first or above the last, theirs.
export function radarColor(stops, dbz) {
  const s = [...(stops?.length ? stops : DEFAULT_STOPS)].sort((a, b) => a.dbz - b.dbz);
  if (dbz <= s[0].dbz) return s[0].color;
  const j = s.findIndex((x) => x.dbz >= dbz);
  if (j < 0) return s[s.length - 1].color;
  const [a, b] = [s[j - 1], s[j]];
  if (b.dbz === a.dbz || b.dbz === dbz) return b.color;   // on a stop (or two stops at one dBZ): that stop's color
  const k = (dbz - a.dbz) / (b.dbz - a.dbz);
  return `rgb(${hex(a.color).map((x, i) => Math.round(x + (hex(b.color)[i] - x) * k)).join(",")})`;
}
// What a level is drawn in, and a storm cell of that strength: the color at the floor of its level, so each stop's
// color is exactly what shows for the level it starts, and the levels between stops blend. null: no dBZ known.
export const levelColor = (stops, dbz) => (dbz == null ? null : radarColor(stops, LOWEST + Math.max(0, Math.floor((dbz - LOWEST) / BAND)) * BAND));
// The legend: each level's color at its place, blended between, as the map's blur blends them.
export function radarScale(stops) {
  const n = (HIGHEST - LOWEST) / BAND + 1, at = (i) => `${((i / (n - 1)) * 100).toFixed(2)}%`;
  return `linear-gradient(90deg, ${Array.from({ length: n }, (_, i) => `${levelColor(stops, LOWEST + i * BAND)} ${at(i)}`).join(", ")})`;
}

const mx = (lon) => (lon + 180) / 360;
const my = (lat) => {
  const s = Math.sin((Math.max(-85.05, Math.min(85.05, lat)) * Math.PI) / 180);
  return 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
};

export const mercator = (lat, lon) => [mx(lon), my(lat)];

// One poly (weather/shapes.py: [w, s, e, n, ring...] in whole 1/q degrees, each ring's points after the first a step
// from the one before) -> {box, rings}, in Web Mercator. The radar's and the watches and warnings' alike.
export function decodePoly(p, q) {
  return {
    box: [mx(p[0] / q), my(p[3] / q), mx(p[2] / q), my(p[1] / q)],     // left, top, right, bottom
    rings: p.slice(4).map((r) => {
      const out = new Float32Array(r.length);
      let x = 0, y = 0;
      for (let i = 0; i < r.length; i += 2) {
        x += r[i]; y += r[i + 1];
        out[i] = mx(x / q); out[i + 1] = my(y / q);
      }
      return out;
    }),
  };
}

// {level, q, bands: [{dbz, polys: [[w, s, e, n, ring...]]}]} -> {level, bands: [{dbz, polys: [{box, rings}]}]}
export function decode(shapes) {
  if (!shapes?.bands) return null;
  return { level: shapes.level, bands: shapes.bands.map((b) => ({ dbz: b.dbz, polys: b.polys.map((p) => decodePoly(p, shapes.q)) })) };
}

// A Leaflet layer in `pane`: set(shapes, colorOf) shows a decoded frame (colorOf(dbz) -> CSS color), setOpacity()
// fades it as a whole (the levels are stacked, so they're drawn opaque and the canvas is faded).
export function radarCanvas(L, pane, attribution) {
  const Layer = L.Layer.extend({
    options: { pane, attribution },
    ...mapCanvas(L, "wxradar-canvas"),
    set(shapes, colorOf) {
      this._shapes = shapes;
      this._colorOf = colorOf;
      this._draw();
      return this;
    },
    setOpacity(o) {
      this._opacity = o;
      if (this._canvas) this._canvas.style.opacity = o;
      return this;
    },
    _paint(ctx, corner, size, zoom) {
      if (!this._shapes) return;
      const world = 256 * 2 ** zoom;
      const x0 = corner.x / world, y0 = corner.y / world, x1 = x0 + size.x / world, y1 = y0 + size.y / world;
      // Blurred about half a ~2 km pixel (1-8 screen pixels), so the levels blend into each other instead of stepping.
      ctx.filter = `blur(${Math.min(8, Math.max(1, (0.01 / 360) * world)) * this._ratio}px)`;
      for (const band of this._shapes.bands) {               // lowest first: each level drawn over the one below
        ctx.fillStyle = this._colorOf(band.dbz);
        ctx.beginPath();
        for (const p of band.polys) {
          if (p.box[2] < x0 || p.box[0] > x1 || p.box[3] < y0 || p.box[1] > y1) continue;
          for (const r of p.rings) {
            ctx.moveTo((r[0] - x0) * world, (r[1] - y0) * world);
            for (let i = 2; i < r.length; i += 2) ctx.lineTo((r[i] - x0) * world, (r[i + 1] - y0) * world);
            ctx.closePath();
          }
        }
        ctx.fill("evenodd");                                 // a polygon's holes, and only its own
      }
    },
  });
  return new Layer();
}
