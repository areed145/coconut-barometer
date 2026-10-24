// The Weather map's lightning (#94): where it struck, from GOES-East's lightning mapper (weather/glm.py), counted per
// minute and cell. At the time the map shows, the flashes of the TRAIL minutes up to it are drawn on one canvas
// (map_canvas.js): a dot per cell and minute, bigger the more flashes, white when new and fading through yellow and
// orange to red as it ages, the newest on top.

import { mapCanvas } from "./map_canvas.js";
import { rampColor, rampGradient } from "./ramp.js";

export const TRAIL = 15 * 60;          // seconds of lightning shown up to the map's time
export const AGE_STOPS = [[0, "#ffffff"], [2, "#fff36b"], [5, "#ffb020"], [10, "#f0582a"], [15, "#b3122e"]]
  .map(([min, color]) => ({ at: min * 60, color }));
export const lightningScale = () => rampGradient(AGE_STOPS, 0, TRAIL);

// The minutes held (minute -> flat [lat, lon, n, ...] in whole 1/q degrees, each cell's south west corner) merged with
// a /api/wx/glm answer, keeping those since `oldest`. A minute in the answer replaces the one held (its last files
// may have come in since).
export function mergeMinutes(held, answer, oldest) {
  const out = new Map([...held].filter(([m]) => m >= oldest));
  for (const [m, cells] of answer?.minutes || []) if (m >= oldest) out.set(m, cells);
  return out;
}

// A Leaflet layer in `pane`: set(minutes, q, t) shows the cells of the TRAIL up to t (epoch seconds).
export function lightningCanvas(L, pane, attribution) {
  const Layer = L.Layer.extend({
    options: { pane, attribution },
    ...mapCanvas(L, "wxlightning-canvas"),
    set(minutes, q, t) {
      this._data = minutes && q && t ? { minutes, q, t } : null;
      this._draw();
      return this;
    },
    _paint(ctx, corner, size, zoom) {
      if (!this._data) return;
      const { minutes, q, t } = this._data, map = this._map;
      const view = L.latLngBounds(map.unproject(corner, zoom), map.unproject(corner.add(size), zoom)).pad(0.05);
      const base = zoom >= 8 ? 2.5 : zoom >= 6 ? 2 : 1.5;
      const shown = [...minutes.keys()].filter((m) => m <= t && m > t - TRAIL).sort((a, b) => a - b);   // oldest first
      ctx.lineWidth = 0.75;
      ctx.strokeStyle = "rgba(0, 0, 0, 0.55)";
      for (const m of shown) {
        ctx.fillStyle = rampColor(AGE_STOPS, Math.max(0, t - m - 30));
        const cells = minutes.get(m);
        for (let i = 0; i < cells.length; i += 3) {
          const lat = (cells[i] + 0.5) / q, lon = (cells[i + 1] + 0.5) / q;
          if (!view.contains([lat, lon])) continue;
          const p = map.project([lat, lon], zoom).subtract(corner);
          ctx.beginPath();
          ctx.arc(p.x, p.y, base + Math.min(5, Math.sqrt(cells[i + 2]) - 1), 0, 2 * Math.PI);
          ctx.fill();
          ctx.stroke();
        }
      }
    },
  });
  return new Layer();
}
