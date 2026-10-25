// The Weather map's lightning (#94): where it struck, from GOES-East's lightning mapper (weather/glm.py), counted per
// minute and cell. At the time the map shows, the flashes of the TRAIL minutes up to it are drawn on one canvas
// (map_canvas.js), colored by age from Settings → Display → Lightning colors (white when new, through yellow and
// orange to red, by default), in one of two styles:
//   blobs  the flashes of each BAND-minute band of ages spread into a density (each over ~SIGMA_KM, about a GLM
//          pixel) and cut at two levels: a translucent halo wherever there was a flash at all (EDGE is under one
//          flash's peak, so a lone strike still shows) and an opaque core where they crowd (CORE), each band edged
//          in dark and the newest on top, so a storm is its newest core trailing older halos the way it came. The
//          density is worked out on a grid of CELL screen pixels, only around the flashes, and scaled up smoothly.
//          The last NEW_S seconds' flashes are marked on top as small crosses, exactly where they were.
//   dots   a dot per cell and minute, bigger the more flashes, the newest on top.
//   smooth the server's shapes (weather/glm.py shapes()), drawn as the radar's are (radar_layer.js); this layer then
//          only marks the last NEW_S seconds' flashes on top: solid dots in the newest color, no border, a little
//          bigger where there were more.

import { mapCanvas } from "./map_canvas.js";
import { rampColor, rampRGB, rampGradient } from "./ramp.js";

export const TRAIL = 15 * 60;          // seconds of lightning shown up to the map's time
// config.py's theme.lightning_colors: minutes ago -> color.
export const DEFAULT_STOPS = [[0, "#ffffff"], [2, "#fff36b"], [5, "#ffb020"], [10, "#f0582a"], [15, "#b3122e"]]
  .map(([min, color]) => ({ min, color }));
// The stops as ramp.js takes them, in seconds; none (the server's not answered yet): the defaults.
export const ageStops = (stops) => (stops?.length ? stops : DEFAULT_STOPS).map((s) => ({ at: s.min * 60, color: s.color }));
export const lightningScale = (stops) => rampGradient(ageStops(stops), 0, TRAIL);

const BAND = 3 * 60;                   // blobs: seconds of ages per band (five bands over the trail)
const CELL = 2;                        // screen px per density cell
const SIGMA_KM = 8, SIGMA_MIN = 3;     // a flash's spread: ~a GLM pixel, but at least this many cells
const EDGE = 0.4, CORE = 4;            // the cuts, in one lone flash's peak density: the halo, the core
const HALO_A = 0.5, CORE_A = 0.9, EDGE_RGBA = [24, 12, 12, 0.45];
const NEW_S = 120;                     // the newest flashes, marked on top

// The minutes held (minute -> flat [lat, lon, n, ...] in whole 1/q degrees, each cell's south west corner) merged with
// a /api/wx/glm answer, keeping those since `oldest`. A minute in the answer replaces the one held (its last files
// may have come in since).
export function mergeMinutes(held, answer, oldest) {
  const out = new Map([...held].filter(([m]) => m >= oldest));
  for (const [m, cells] of answer?.minutes || []) if (m >= oldest) out.set(m, cells);
  return out;
}

// ---- the density ------------------------------------------------------------------------------------------------
// A box blur of radius r along rows (step 1) or columns (step nx) of the rectangle [x0, x1] × [y0, y1], src -> dst.
// Three of them each way are close to a Gaussian.
function box(src, dst, nx, x0, x1, y0, y1, r, rows) {
  const w = 2 * r + 1;
  const [a0, a1, b0, b1, step, stride] = rows ? [x0, x1, y0, y1, 1, nx] : [y0, y1, x0, x1, nx, 1];
  for (let b = b0; b <= b1; b++) {
    const base = b * stride;
    let s = 0;
    for (let a = a0; a <= Math.min(a1, a0 + r); a++) s += src[base + a * step];
    for (let a = a0; a <= a1; a++) {
      dst[base + a * step] = s / w;
      if (a + r + 1 <= a1) s += src[base + (a + r + 1) * step];
      if (a - r >= a0) s -= src[base + (a - r) * step];
    }
  }
}
export function blur(grid, tmp, nx, rect, r) {
  const [x0, x1, y0, y1] = rect;
  for (let i = 0; i < 3; i++) { box(grid, tmp, nx, x0, x1, y0, y1, r, true); box(tmp, grid, nx, x0, x1, y0, y1, r, false); }
}
// One lone flash's peak after blur(): the centre of an impulse blurred the same way.
const peaks = new Map();
export function peak(r) {
  if (!peaks.has(r)) {
    const n = 6 * r + 3, g = new Float32Array(n * n), t = new Float32Array(n * n), c = (n - 1) / 2;
    g[c * n + c] = 1;
    blur(g, t, n, [0, n - 1, 0, n - 1], r);
    peaks.set(r, g[c * n + c]);
  }
  return peaks.get(r);
}
// The box radius whose three passes spread like a Gaussian of `sigma` cells.
export const radiusFor = (sigma) => Math.max(1, Math.round((Math.sqrt(4 * sigma * sigma + 1) - 1) / 2));

// `color` [r, g, b] at alpha a over pixel i of img (straight alpha).
function over(d, i, [r, g, b], a) {
  const da = d[i + 3] / 255, oa = a + da * (1 - a);
  if (oa <= 0) return;
  const k = da * (1 - a);
  d[i] = (r * a + d[i] * k) / oa;
  d[i + 1] = (g * a + d[i + 1] * k) / oa;
  d[i + 2] = (b * a + d[i + 2] * k) / oa;
  d[i + 3] = oa * 255;
}

// The blobs onto img (nx × ny cells): bands [[rgb, [[x, y, n], ...]], ...], oldest first; r the blur radius.
export function paintBlobs(img, nx, ny, bands, r) {
  const grid = new Float32Array(nx * ny), tmp = new Float32Array(nx * ny), p = peak(r), d = img.data;
  const edge = EDGE * p, core = CORE * p, m = 3 * r + 2;
  for (const [rgb, pts] of bands) {
    if (!pts.length) continue;
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const [x, y, n] of pts) {
      if (x < 0 || y < 0 || x >= nx || y >= ny) continue;
      grid[y * nx + x] += n;
      x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
    }
    if (x0 > x1) continue;
    const rect = [Math.max(0, x0 - m), Math.min(nx - 1, x1 + m), Math.max(0, y0 - m), Math.min(ny - 1, y1 + m)];
    blur(grid, tmp, nx, rect, r);
    const [a0, a1, b0, b1] = rect;
    const at = (x, y) => (x < a0 || x > a1 || y < b0 || y > b1 ? 0 : grid[y * nx + x]);
    for (let y = b0; y <= b1; y++) {
      for (let x = a0; x <= a1; x++) {
        const v = grid[y * nx + x];
        if (v < edge) continue;
        const i = (y * nx + x) * 4;
        if (at(x - 1, y) < edge || at(x + 1, y) < edge || at(x, y - 1) < edge || at(x, y + 1) < edge) {
          over(d, i, EDGE_RGBA, EDGE_RGBA[3]);
        } else {
          over(d, i, rgb, v >= core ? CORE_A : HALO_A);
        }
      }
    }
    for (let y = rect[2]; y <= rect[3]; y++) grid.fill(0, y * nx + rect[0], y * nx + rect[1] + 1);
  }
}

// ---- the layer --------------------------------------------------------------------------------------------------
// A Leaflet layer in `pane`: set(minutes, q, t, stops, style) shows the cells of the TRAIL up to t (epoch seconds),
// colored from `stops` (Settings → Display → Lightning colors), as "blobs" or "dots".
export function lightningCanvas(L, pane, attribution) {
  const Layer = L.Layer.extend({
    options: { pane, attribution },
    ...mapCanvas(L, "wxlightning-canvas"),
    set(minutes, q, t, stops, style = "blobs") {
      this._data = minutes && q && t ? { minutes, q, t, stops: ageStops(stops), style } : null;
      this._draw();
      return this;
    },
    // [[age, [lat, lon, n, ...]], ...] in the trail, oldest first.
    _shown() {
      const { minutes, t } = this._data;
      return [...minutes.keys()].filter((m) => m <= t && m > t - TRAIL).sort((a, b) => a - b)
        .map((m) => [Math.max(0, t - m - 30), minutes.get(m)]);
    },
    _paint(ctx, corner, size, zoom) {
      if (!this._data) return;
      const { q, stops, style } = this._data, map = this._map;
      const view = L.latLngBounds(map.unproject(corner, zoom), map.unproject(corner.add(size), zoom)).pad(0.05);
      const each = (cells, fn) => {
        for (let i = 0; i < cells.length; i += 3) {
          const lat = (cells[i] + 0.5) / q, lon = (cells[i + 1] + 0.5) / q;
          if (!view.contains([lat, lon])) continue;
          const p = map.project([lat, lon], zoom).subtract(corner);
          fn(p.x, p.y, cells[i + 2]);
        }
      };
      const shown = this._shown();
      ctx.strokeStyle = "rgba(0, 0, 0, 0.55)";
      if (style === "smooth") {
        // Small zoomed out, where a storm is a few px and its flashes would cover it; bigger in.
        const [base, grow] = zoom >= 8 ? [4, 2] : zoom >= 6 ? [3.5, 1.5] : zoom >= 5 ? [2.5, 1] : [1.75, 0.5];
        ctx.fillStyle = rampColor(stops, 0);
        ctx.beginPath();
        for (const [, cells] of shown.filter(([age]) => age < NEW_S)) {
          each(cells, (x, y, n) => {
            const r = base + Math.min(grow, (Math.sqrt(n) - 1) / 2);
            ctx.moveTo(x + r, y);
            ctx.arc(x, y, r, 0, 2 * Math.PI);
          });
        }
        ctx.fill();
        return;
      }
      if (style === "dots") {
        const base = zoom >= 8 ? 2.5 : zoom >= 6 ? 2 : 1.5;
        ctx.lineWidth = 0.75;
        for (const [age, cells] of shown) {
          ctx.fillStyle = rampColor(stops, age);
          each(cells, (x, y, n) => {
            ctx.beginPath();
            ctx.arc(x, y, base + Math.min(5, Math.sqrt(n) - 1), 0, 2 * Math.PI);
            ctx.fill();
            ctx.stroke();
          });
        }
        return;
      }
      // Blobs: each band's flashes in density cells, the oldest band first.
      const nx = Math.ceil(size.x / CELL), ny = Math.ceil(size.y / CELL);
      const kmPerPx = 40075 * Math.cos((map.unproject(corner.add(size.divideBy(2)), zoom).lat * Math.PI) / 180) / (256 * 2 ** zoom);
      const r = radiusFor(Math.max(SIGMA_MIN, SIGMA_KM / kmPerPx / CELL));
      const bands = [];
      for (let b = Math.ceil(TRAIL / BAND) - 1; b >= 0; b--) {
        const pts = [];
        for (const [age, cells] of shown) {
          if (Math.min(Math.floor(age / BAND), TRAIL / BAND - 1) !== b) continue;
          each(cells, (x, y, n) => pts.push([Math.floor(x / CELL), Math.floor(y / CELL), n]));
        }
        bands.push([rampRGB(stops, (b + 0.5) * BAND), pts]);
      }
      if (!this._buffer) this._buffer = document.createElement("canvas");
      const buf = this._buffer;
      buf.width = nx; buf.height = ny;
      const bctx = buf.getContext("2d"), img = bctx.createImageData(nx, ny);
      paintBlobs(img, nx, ny, bands, r);
      bctx.putImageData(img, 0, 0);
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(buf, 0, 0, nx * CELL, ny * CELL);
      // The newest flashes, exactly where they were: a small cross apiece, light over a dark outline.
      const fresh = shown.filter(([age]) => age < NEW_S);
      for (const [width, color] of [[3, "rgba(0, 0, 0, 0.6)"], [1.25, rampColor(stops, 0)]]) {
        ctx.lineWidth = width;
        ctx.strokeStyle = color;
        ctx.beginPath();
        for (const [, cells] of fresh) each(cells, (x, y) => { ctx.moveTo(x - 3, y); ctx.lineTo(x + 3, y); ctx.moveTo(x, y - 3); ctx.lineTo(x, y + 3); });
        ctx.stroke();
      }
    },
  });
  return new Layer();
}
