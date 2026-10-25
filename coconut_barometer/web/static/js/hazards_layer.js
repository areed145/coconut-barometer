// The Weather map's watches, warnings and advisories (#87), the NWS's in effect now (weather/hazards.py): each alert's
// areas filled translucent in the NWS hazard map's own color and outlined, warnings more boldly. The server sends them
// most important first; they're drawn the other way round, so a tornado warning is on top of the watch it's in, and a
// tap finds the most important first. Shapes come as radar's do (radar_layer.js decodePoly()), drawn on one canvas
// (map_canvas.js). Advisories, statements and marine alerts (`minor`) are drawn only when asked.

import { mapCanvas } from "./map_canvas.js";
import { decodePoly, mercator } from "./radar_layer.js";

const FILL = 0.22, WIDTH = { W: 2.5, A: 2 }, THIN = 1.25;   // fill opacity; outline px by significance, else THIN

// /api/wx/hazards -> {level, alerts: [{id, event, sig, color, minor, ends, expires, wfo, polys: [{box, rings}]}]}
export function decodeHazards(x) {
  if (!x?.shapes?.alerts) return null;
  const q = x.shapes.q;
  return { level: x.shapes.level, alerts: x.shapes.alerts.map((a) => ({ ...a, polys: a.polys.map((p) => decodePoly(p, q)) })) };
}

// Is (x, y) inside the poly's rings (even-odd, so not in its holes)? In Web Mercator, as it's drawn.
function inside(p, x, y) {
  if (x < p.box[0] || x > p.box[2] || y < p.box[1] || y > p.box[3]) return false;
  let odd = false;
  for (const r of p.rings) {
    for (let i = 0, j = r.length - 2; i < r.length; j = i, i += 2) {
      const xi = r[i], yi = r[i + 1], xj = r[j], yj = r[j + 1];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) odd = !odd;
    }
  }
  return odd;
}

// The alerts shown that cover (lat, lon), most important first.
export function alertsAt(alerts, lat, lon, minor) {
  const [x, y] = mercator(lat, lon);
  return (alerts || []).filter((a) => (minor || !a.minor) && a.polys.some((p) => inside(p, x, y)));
}

// A Leaflet layer in `pane`: set(decoded, minor) shows decodeHazards()'s alerts (the minor ones too if `minor`);
// at(latlng) is what a tap there opens.
export function hazardsCanvas(L, pane, attribution) {
  const Layer = L.Layer.extend({
    options: { pane, attribution },
    ...mapCanvas(L, "wxhazards-canvas"),
    set(decoded, minor) {
      this._alerts = decoded?.alerts || [];
      this._minor = minor;
      this._draw();
      return this;
    },
    at(latlng) { return alertsAt(this._alerts, latlng.lat, latlng.lng, this._minor); },
    _paint(ctx, corner, size, zoom) {
      if (!this._alerts) return;
      const world = 256 * 2 ** zoom;
      const x0 = corner.x / world, y0 = corner.y / world, x1 = x0 + size.x / world, y1 = y0 + size.y / world;
      ctx.lineJoin = "round";
      for (let k = this._alerts.length - 1; k >= 0; k--) {     // least important first: the most important on top
        const a = this._alerts[k];
        if (a.minor && !this._minor) continue;
        ctx.beginPath();
        let any = false;
        for (const p of a.polys) {
          if (p.box[2] < x0 || p.box[0] > x1 || p.box[3] < y0 || p.box[1] > y1) continue;
          any = true;
          for (const r of p.rings) {
            ctx.moveTo((r[0] - x0) * world, (r[1] - y0) * world);
            for (let i = 2; i < r.length; i += 2) ctx.lineTo((r[i] - x0) * world, (r[i + 1] - y0) * world);
            ctx.closePath();
          }
        }
        if (!any) continue;
        ctx.globalAlpha = FILL;
        ctx.fillStyle = a.color;
        ctx.fill("evenodd");
        ctx.globalAlpha = 1;
        ctx.strokeStyle = a.color;
        ctx.lineWidth = WIDTH[a.sig] || THIN;
        ctx.stroke();
      }
    },
  });
  return new Layer();
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

const when = (ts) => {
  const d = new Date(ts * 1000), today = new Date().toDateString() === d.toDateString();
  return d.toLocaleString(undefined, today ? { hour: "numeric", minute: "2-digit" } : { weekday: "short", hour: "numeric", minute: "2-digit" });
};
const MORE = 3;                      // alerts listed in one popup; the rest are counted

// A tap's popup (built as elements, for the CSP): each alert there, what it is and until when; the most important's
// area and what to do, fetched with text(id) -> {area, sender, instruction}; and weather.gov's page for that spot,
// which has every alert there in full. loaded() is called once that text is in.
export function hazardPopup(hits, lat, lon, text, loaded) {
  const box = el("div", "wxpop wxhazpop");
  for (const a of hits.slice(0, MORE)) {
    const head = el("b", "wxhaz");
    const swatch = el("i");
    swatch.style.background = a.color;
    head.append(swatch, a.event);
    const end = a.ends || a.expires;
    box.append(head, el("div", "muted", [end && `until ${when(end)}`, a.wfo && `NWS ${a.wfo.replace(/^K/, "")}`].filter(Boolean).join(" · ")));
  }
  if (hits.length > MORE) box.append(el("div", "muted", `and ${hits.length - MORE} more`));
  const more = el("div", "wxhaztext");
  box.append(more);
  text(hits[0].id).then((d) => {
    if (d.area) more.append(el("div", null, d.area));
    if (d.instruction) more.append(el("p", null, d.instruction.replace(/\s+/g, " ").slice(0, 400) + (d.instruction.length > 400 ? "…" : "")));
    loaded?.();
  }).catch(() => {});
  const link = el("a", null, "Full text on weather.gov →");
  link.href = `https://forecast.weather.gov/MapClick.php?lat=${lat.toFixed(4)}&lon=${lon.toFixed(4)}`;
  link.target = "_blank";
  link.rel = "noopener";
  box.append(link);
  return box;
}
