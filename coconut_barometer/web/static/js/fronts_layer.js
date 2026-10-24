// The Weather map's surface fronts and highs and lows (#69), from WPC's analysis (weather/fronts.py), drawn on one
// canvas with the usual symbols, spaced on the screen so they stay even at any zoom:
//   cold        blue, triangles          warm        red, half circles
//   stationary  red and blue in turn, triangles one side and half circles the other
//   occluded    purple, triangles and half circles in turn, same side
//   trough      brown, dashed
// The symbols go on the side the front is moving toward: the left, walking its points in order.

const COLORS = { cold: "#1f5fd6", warm: "#d63030", occluded: "#8a3fc0", trough: "#9a6a2f" };
const STEP = 46, SIZE = 7, WIDTH = 2.5;   // px between symbols, their size, the line's width

// The points of a line in screen pixels -> evenly spaced places along it: {x, y, tx, ty} (tx, ty: unit tangent).
function along(pts, step, first) {
  const out = [];
  let next = first;
  let walked = 0;
  for (let i = 1; i < pts.length; i++) {
    const [ax, ay] = pts[i - 1], [bx, by] = pts[i], len = Math.hypot(bx - ax, by - ay);
    if (!len) continue;
    while (next <= walked + len) {
      const k = (next - walked) / len;
      out.push({ x: ax + (bx - ax) * k, y: ay + (by - ay) * k, tx: (bx - ax) / len, ty: (by - ay) / len });
      next += step;
    }
    walked += len;
  }
  return out;
}

// A symbol at p on the left of the line (screen y points down, so left of (tx, ty) is (ty, -tx)).
function triangle(ctx, p, size) {
  const nx = p.ty, ny = -p.tx;
  ctx.beginPath();
  ctx.moveTo(p.x - p.tx * size, p.y - p.ty * size);
  ctx.lineTo(p.x + nx * size * 1.1, p.y + ny * size * 1.1);
  ctx.lineTo(p.x + p.tx * size, p.y + p.ty * size);
  ctx.closePath();
  ctx.fill();
}
function halfCircle(ctx, p, size, right = false) {
  const a = Math.atan2(p.ty, p.tx);
  ctx.beginPath();
  ctx.arc(p.x, p.y, size * 0.85, a, a + Math.PI, !right);   // through the left (or right) side
  ctx.closePath();
  ctx.fill();
}

function drawFront(ctx, f, pts) {
  ctx.lineWidth = WIDTH;
  ctx.setLineDash(f.type === "trough" ? [8, 6] : []);
  if (f.type === "stationary") {                            // red and blue in turn, one STEP each
    const end = pts[pts.length - 1], marks = [...along(pts, STEP / 2, 0), { x: end[0], y: end[1] }];
    for (let i = 0; i + 1 < marks.length; i++) {
      ctx.strokeStyle = i % 2 ? COLORS.warm : COLORS.cold;
      ctx.beginPath();
      ctx.moveTo(marks[i].x, marks[i].y);
      ctx.lineTo(marks[i + 1].x, marks[i + 1].y);
      ctx.stroke();
    }
  } else {
    ctx.strokeStyle = COLORS[f.type];
    ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.stroke();
  }
  ctx.setLineDash([]);
  if (f.type === "trough") return;
  // Stationary: a symbol in the middle of each colored piece (STEP / 2 long); the others: one every STEP.
  (f.type === "stationary" ? along(pts, STEP / 2, STEP / 4) : along(pts, STEP, STEP / 2)).forEach((p, i) => {
    if (f.type === "cold") { ctx.fillStyle = COLORS.cold; triangle(ctx, p, SIZE); }
    else if (f.type === "warm") { ctx.fillStyle = COLORS.warm; halfCircle(ctx, p, SIZE); }
    else if (f.type === "occluded") { ctx.fillStyle = COLORS.occluded; (i % 2 ? halfCircle : triangle)(ctx, p, SIZE); }
    else if (i % 2) { ctx.fillStyle = COLORS.warm; halfCircle(ctx, p, SIZE, true); }      // stationary: warm side
    else { ctx.fillStyle = COLORS.cold; triangle(ctx, p, SIZE); }                        // ... and cold side
  });
}

function drawCenter(ctx, c, x, y, halo) {
  ctx.textAlign = "center";
  ctx.lineJoin = "round";
  ctx.strokeStyle = halo;
  ctx.fillStyle = c.kind === "H" ? COLORS.cold : COLORS.warm;
  ctx.lineWidth = 4;
  ctx.font = "700 22px system-ui, sans-serif";
  ctx.strokeText(c.kind, x, y + 6);
  ctx.fillText(c.kind, x, y + 6);
  ctx.font = "600 11px system-ui, sans-serif";
  ctx.lineWidth = 3;
  ctx.strokeText(String(c.mb), x, y + 20);
  ctx.fillText(String(c.mb), x, y + 20);
}

// Everything in `analysis` that `inView([lat, lon])` keeps, onto ctx; px([lat, lon]) -> [x, y] screen pixels.
export function paint(ctx, analysis, px, inView, halo) {
  for (const f of analysis.fronts) {
    if (!f.points.some(inView)) continue;
    drawFront(ctx, f, f.points.map(px));
  }
  for (const c of analysis.centers) {
    if (!inView([c.lat, c.lon])) continue;
    const [x, y] = px([c.lat, c.lon]);
    drawCenter(ctx, c, x, y, halo);
  }
}

// A Leaflet layer in `pane`: set(analysis) shows /api/wx/fronts's {fronts, centers}; `halo` is the color behind
// the H and L letters (the map's background).
export function frontsCanvas(L, pane, attribution, halo = "#fff") {
  const Layer = L.Layer.extend({
    options: { pane, attribution },
    onAdd(map) {
      this._canvas = L.DomUtil.create("canvas", "wxfronts-canvas", map.getPane(pane));
      map.on("moveend resize", this._reset, this).on("zoomstart", this._hide, this);
      this._reset();
    },
    onRemove(map) {
      map.off("moveend resize", this._reset, this).off("zoomstart", this._hide, this);
      L.DomUtil.remove(this._canvas);
      this._canvas = null;
    },
    set(analysis, haloColor) {
      this._data = analysis;
      if (haloColor) halo = haloColor;
      if (this._canvas) this._draw();
      return this;
    },
    _hide() { this._canvas.style.visibility = "hidden"; },
    _reset() {
      const map = this._map, size = map.getSize(), ratio = window.devicePixelRatio || 1, c = this._canvas;
      L.DomUtil.setPosition(c, map.containerPointToLayerPoint([0, 0]));
      c.width = Math.round(size.x * ratio);
      c.height = Math.round(size.y * ratio);
      c.style.width = `${size.x}px`;
      c.style.height = `${size.y}px`;
      c.style.visibility = "visible";
      this._draw();
    },
    _draw() {
      const c = this._canvas, ctx = c.getContext("2d"), map = this._map, ratio = c.width / map.getSize().x;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, c.width, c.height);
      if (!this._data) return;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      const view = map.getBounds().pad(0.2);
      const px = ([lat, lon]) => { const p = map.latLngToContainerPoint([lat, lon]); return [p.x, p.y]; };
      paint(ctx, this._data, px, ([lat, lon]) => view.contains([lat, lon]), halo);
    },
  });
  return new Layer();
}
