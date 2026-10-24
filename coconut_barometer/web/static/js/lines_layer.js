// The Weather map's coastlines, lakes and borders over the satellite (#95), whose imagery hides the basemap's own:
// Natural Earth's 1:50m lines (public domain; tools/natural_earth_lines.py makes the file), loaded the first time
// they're shown, drawn on one canvas (map_canvas.js) light over a dark halo so they show on cloud and ground alike.
// The place names stay readable over it all as the basemap's own labels tiles (leaflet.js baseMap's labels).

import { mapCanvas } from "./map_canvas.js";

// name -> [line width, alpha]: the coasts and borders firmer than the states and lakes.
const STYLES = { lake: [0.8, 0.55], state: [0.8, 0.6], coast: [1.1, 0.8], country: [1.3, 0.85] };

let loading = null;
// The lines as {q, layers: {name: [[x0, y0, dx, dy, ...], ...]}} (lon, lat in whole 1/q degrees), fetched once.
export function loadLines() {
  if (!loading) loading = fetch(new URL("../vendor/natural-earth-lines.json", import.meta.url))
    .then((r) => { if (!r.ok) throw new Error(`lines: HTTP ${r.status}`); return r.json(); })
    .catch((e) => { loading = null; throw e; });
  return loading;
}

// A line's points as [lat, lon] pairs, and its bounds [west, south, east, north].
export function decodeLine(enc, q) {
  const pts = [];
  let x = 0, y = 0, w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
  for (let i = 0; i + 1 < enc.length; i += 2) {
    x += enc[i]; y += enc[i + 1];
    const lon = x / q, lat = y / q;
    pts.push([lat, lon]);
    w = Math.min(w, lon); e = Math.max(e, lon); s = Math.min(s, lat); n = Math.max(n, lat);
  }
  return { pts, bounds: [w, s, e, n] };
}

// A Leaflet layer in `pane`: set(lines) draws loadLines()'s lines (decoded once here).
export function linesCanvas(L, pane, attribution) {
  const Layer = L.Layer.extend({
    options: { pane, attribution },
    ...mapCanvas(L, "wxlines-canvas"),
    set(lines) {
      if (lines !== this._raw) {
        this._raw = lines;
        this._lines = lines ? Object.entries(STYLES).map(([name, style]) =>
          [style, (lines.layers[name] || []).map((enc) => decodeLine(enc, lines.q))]) : null;
      }
      this._draw();
      return this;
    },
    _paint(ctx, corner, size, zoom) {
      if (!this._lines) return;
      const map = this._map;
      const view = L.latLngBounds(map.unproject(corner, zoom), map.unproject(corner.add(size), zoom));
      const [vw, vs, ve, vn] = [view.getWest(), view.getSouth(), view.getEast(), view.getNorth()];
      ctx.lineJoin = ctx.lineCap = "round";
      for (const halo of [true, false]) {
        for (const [[width, alpha], lines] of this._lines) {
          ctx.lineWidth = halo ? width + 2 : width;
          ctx.strokeStyle = halo ? `rgba(0, 0, 0, ${alpha * 0.45})` : `rgba(255, 255, 255, ${alpha})`;
          ctx.beginPath();
          for (const { pts, bounds: [w, s, e, n] } of lines) {
            if (e < vw || w > ve || n < vs || s > vn) continue;
            pts.forEach(([lat, lon], i) => {
              const p = map.project([lat, lon], zoom).subtract(corner);
              if (i) ctx.lineTo(p.x, p.y); else ctx.moveTo(p.x, p.y);
            });
          }
          ctx.stroke();
        }
      }
    },
  });
  return new Layer();
}
