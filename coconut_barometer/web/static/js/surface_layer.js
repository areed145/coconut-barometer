// The Weather map's surface analysis (weather/rtma.py: NOAA RTMA, every 15 minutes): the air temperature 2 m up as a
// smooth color field (#85), and the wind 10 m up as arrows (#86). Both come as one regular lat/lon grid (decodeGrid())
// read bilinearly wherever it's needed (sample()), each drawn on its own canvas (map_canvas.js):
//   temperature  colored at every few screen pixels and scaled up smoothly, so it's as fine as the screen at any zoom
//   wind         an arrow every SPACING screen pixels (so as many at any zoom), the way the wind blows, longer and
//                warmer-colored the stronger it is; under 1 m/s, a small circle (calm)

import { mapCanvas } from "./map_canvas.js";
import { rampColor, rampRGB, rampGradient } from "./ramp.js";

// The temperature's colors, in °F (where its familiar thresholds are) and kept in °C: purple cold, blue to freezing,
// green mild, yellow to red hot.
export const TEMP_STOPS = [[-20, "#6a1b9a"], [0, "#3949ab"], [20, "#1e88e5"], [32, "#4fc3f7"], [45, "#26a69a"], [55, "#66bb6a"],
  [65, "#d4e157"], [75, "#fdd835"], [85, "#fb8c00"], [95, "#e53935"], [105, "#ad1457"], [115, "#6d0f2a"]]
  .map(([f, color]) => ({ at: (f - 32) / 1.8, color }));
// The wind's, in m/s: grey calm, blue and green light, yellow to red strong.
export const WIND_STOPS = [[0, "#90a4ae"], [2, "#4fc3f7"], [5, "#43a047"], [8, "#fdd835"], [11, "#fb8c00"], [15, "#e53935"], [20, "#ad1457"]]
  .map(([at, color]) => ({ at, color }));
export const TEMP_RANGE = [-30, 45], WIND_RANGE = [0, 20];      // the legends' ends (°C, m/s)

const CELL = 4;                       // temperature: screen px between the points colored (then scaled up smoothly)
const LUT_LO = -60, LUT_STEP = 0.25, LUT_N = 481;               // a color per quarter °C, -60 to 60
const SPACING = 38, SPACING_PHONE = 46, CALM = 1;              // wind: px between arrows; m/s under which it's calm

// {lat0, lon0, dlat, dlon, nx, ny, scale, missing, fields: {name: base64 int16}} -> the same with fields as
// Float32Arrays (NaN for none); null without a grid.
export function decodeGrid(x) {
  if (!x?.nx || !x.ny || !x.fields) return null;
  const fields = {};
  for (const [k, b64] of Object.entries(x.fields)) {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const raw = new Int16Array(bytes.buffer), out = new Float32Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw[i] === x.missing ? NaN : raw[i] * x.scale;
    fields[k] = out;
  }
  return { valid: x.valid, lat0: x.lat0, lon0: x.lon0, dlat: x.dlat, dlon: x.dlon, nx: x.nx, ny: x.ny, fields };
}

// Field `name` at (lat, lon), bilinear; NaN off the grid or next to a point with none.
export function sample(g, name, lat, lon) {
  const fx = (lon - g.lon0) / g.dlon, fy = (lat - g.lat0) / g.dlat;
  if (!(fx >= 0 && fy >= 0 && fx <= g.nx - 1 && fy <= g.ny - 1)) return NaN;
  const i = Math.min(g.nx - 2, Math.floor(fx)), j = Math.min(g.ny - 2, Math.floor(fy)), a = fx - i, b = fy - j;
  const f = g.fields[name], k = j * g.nx + i;
  return (f[k] * (1 - a) + f[k + 1] * a) * (1 - b) + (f[k + g.nx] * (1 - a) + f[k + g.nx + 1] * a) * b;
}

// The legends' gradients, between the ranges' ends.
export const tempScale = () => rampGradient(TEMP_STOPS, ...TEMP_RANGE);
export const windScale = () => rampGradient(WIND_STOPS, ...WIND_RANGE);

const lonAt = (x) => x * 360 - 180;                                    // Web Mercator (0-1 across the world) -> degrees
const latAt = (y) => (Math.atan(Math.sinh(Math.PI * (1 - 2 * y))) * 180) / Math.PI;

let lut = null;
function tempLut() {
  if (lut) return lut;
  lut = new Uint8ClampedArray(LUT_N * 4);
  for (let i = 0; i < LUT_N; i++) {
    const [r, g, b] = rampRGB(TEMP_STOPS, LUT_LO + i * LUT_STEP);
    lut.set([r, g, b, 255], i * 4);
  }
  return lut;
}

// Leaflet layers in `pane`: set(grid) shows decodeGrid()'s grid; the temperature's setOpacity() fades it as a whole.
export function temperatureCanvas(L, pane, attribution) {
  const Layer = L.Layer.extend({
    options: { pane, attribution },
    ...mapCanvas(L, "wxtemp-canvas"),
    set(grid) { this._grid = grid; this._draw(); return this; },
    setOpacity(o) {
      this._opacity = o;
      if (this._canvas) this._canvas.style.opacity = o;
      return this;
    },
    _paint(ctx, corner, size, zoom) {
      const g = this._grid;
      if (!g?.fields.t) return;
      const world = 256 * 2 ** zoom, cols = Math.ceil(size.x / CELL), rows = Math.ceil(size.y / CELL);
      const img = new ImageData(cols, rows), px = img.data, colors = tempLut();
      const lons = Float64Array.from({ length: cols }, (_, c) => lonAt((corner.x + (c + 0.5) * CELL) / world));
      for (let r = 0; r < rows; r++) {
        const lat = latAt((corner.y + (r + 0.5) * CELL) / world);
        for (let c = 0; c < cols; c++) {
          const t = sample(g, "t", lat, lons[c]);
          if (Number.isNaN(t)) continue;
          const k = Math.max(0, Math.min(LUT_N - 1, Math.round((t - LUT_LO) / LUT_STEP))) * 4, o = (r * cols + c) * 4;
          px[o] = colors[k]; px[o + 1] = colors[k + 1]; px[o + 2] = colors[k + 2]; px[o + 3] = 255;
        }
      }
      const off = document.createElement("canvas");
      off.width = cols; off.height = rows;
      off.getContext("2d").putImageData(img, 0, 0);
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(off, 0, 0, cols * CELL, rows * CELL);
    },
  });
  return new Layer();
}

// One arrow at (x, y) for (u, v) m/s; `halo` the map's background, under it so it reads on any color.
export function arrow(ctx, x, y, u, v, spacing, halo) {
  const speed = Math.hypot(u, v), color = rampColor(WIND_STOPS, speed);
  if (speed < CALM) {
    ctx.beginPath();
    ctx.arc(x, y, 2.5, 0, 2 * Math.PI);
    ctx.lineWidth = 3; ctx.strokeStyle = halo; ctx.stroke();
    ctx.lineWidth = 1.25; ctx.strokeStyle = color; ctx.stroke();
    return;
  }
  const len = Math.min(spacing * 0.9, 10 + speed * 2.2), dx = u / speed, dy = -v / speed;   // screen y points down
  const tx = x + (dx * len) / 2, ty = y + (dy * len) / 2, head = Math.min(7, 3 + len / 6);
  const path = () => {
    ctx.beginPath();
    ctx.moveTo(x - (dx * len) / 2, y - (dy * len) / 2);
    ctx.lineTo(tx, ty);
    ctx.moveTo(tx - dx * head - dy * head * 0.6, ty - dy * head + dx * head * 0.6);
    ctx.lineTo(tx, ty);
    ctx.lineTo(tx - dx * head + dy * head * 0.6, ty - dy * head - dx * head * 0.6);
  };
  ctx.lineCap = "round"; ctx.lineJoin = "round";
  path(); ctx.lineWidth = 4; ctx.strokeStyle = halo; ctx.stroke();
  path(); ctx.lineWidth = 2; ctx.strokeStyle = color; ctx.stroke();
}

export function windCanvas(L, pane, attribution, halo = "#fff") {
  const Layer = L.Layer.extend({
    options: { pane, attribution },
    ...mapCanvas(L, "wxwind-canvas"),
    set(grid, haloColor) {
      this._grid = grid;
      if (haloColor) halo = haloColor;
      this._draw();
      return this;
    },
    _paint(ctx, corner, size, zoom) {
      const g = this._grid;
      if (!g?.fields.u) return;
      const world = 256 * 2 ** zoom, step = matchMedia("(max-width: 639px)").matches ? SPACING_PHONE : SPACING;
      // On a grid fixed to the world, not the screen, so the arrows stay put while the map is panned.
      const x0 = Math.ceil(corner.x / step) * step, y0 = Math.ceil(corner.y / step) * step;
      for (let wy = y0; wy < corner.y + size.y; wy += step) {
        const lat = latAt(wy / world);
        for (let wx = x0; wx < corner.x + size.x; wx += step) {
          const lon = lonAt(wx / world), u = sample(g, "u", lat, lon), v = sample(g, "v", lat, lon);
          if (Number.isNaN(u) || Number.isNaN(v)) continue;
          arrow(ctx, wx - corner.x, wy - corner.y, u, v, step, halo);
        }
      }
    },
  });
  return new Layer();
}
