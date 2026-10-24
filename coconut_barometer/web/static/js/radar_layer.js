// The Weather map's smooth radar, drawn on one canvas (#70). The server sends a frame's shapes compactly
// (weather/radar.py encode()): per dBZ level, everything at that level and up, each polygon its bounds and rings of
// whole 1/q degrees, every point after the first the step from the one before. decode() turns that into flat
// Float32Arrays already projected to Web Mercator (0–1 across the world), once per frame; the layer then draws
// whichever frame it's given, culling to the view, so a loop step is one canvas redraw - nothing is rebuilt and no
// Leaflet object is made per polygon.

const mx = (lon) => (lon + 180) / 360;
const my = (lat) => {
  const s = Math.sin((Math.max(-85.05, Math.min(85.05, lat)) * Math.PI) / 180);
  return 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
};

// {level, q, bands: [{dbz, polys: [[w, s, e, n, ring...]]}]} -> {level, bands: [{dbz, polys: [{box, rings}]}]}
export function decode(shapes) {
  if (!shapes?.bands) return null;
  const q = shapes.q;
  return {
    level: shapes.level,
    bands: shapes.bands.map((b) => ({
      dbz: b.dbz,
      polys: b.polys.map((p) => ({
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
      })),
    })),
  };
}

// A Leaflet layer in `pane`: set(shapes, colorOf) shows a decoded frame (colorOf(dbz) -> CSS color), setOpacity()
// fades it as a whole (the levels are stacked, so they're drawn opaque and the canvas is faded).
export function radarCanvas(L, pane, attribution) {
  const Layer = L.Layer.extend({
    options: { pane, attribution },
    onAdd(map) {
      this._canvas = L.DomUtil.create("canvas", "wxradar-canvas", map.getPane(pane));
      this._canvas.style.opacity = this._opacity ?? 1;
      map.on("moveend resize", this._reset, this).on("zoomstart", this._hide, this);
      this._reset();
    },
    onRemove(map) {
      map.off("moveend resize", this._reset, this).off("zoomstart", this._hide, this);
      L.DomUtil.remove(this._canvas);
      this._canvas = null;
    },
    set(shapes, colorOf) {
      this._shapes = shapes;
      this._colorOf = colorOf;
      if (this._canvas) this._draw();
      return this;
    },
    setOpacity(o) {
      this._opacity = o;
      if (this._canvas) this._canvas.style.opacity = o;
      return this;
    },
    // Leaflet scales its own layers while zooming; this one just hides and is drawn again at the new zoom.
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
      const c = this._canvas, ctx = c.getContext("2d"), map = this._map;
      ctx.clearRect(0, 0, c.width, c.height);
      if (!this._shapes) return;
      const size = map.getSize(), world = 256 * 2 ** map.getZoom(), k = world * (c.width / size.x);
      const corner = map.containerPointToLayerPoint([0, 0]).add(map.getPixelOrigin());   // the canvas's top left, in world pixels
      const x0 = corner.x / world, y0 = corner.y / world, x1 = x0 + size.x / world, y1 = y0 + size.y / world;
      for (const band of this._shapes.bands) {               // lowest first: each level drawn over the one below
        ctx.fillStyle = this._colorOf(band.dbz);
        ctx.beginPath();
        for (const p of band.polys) {
          if (p.box[2] < x0 || p.box[0] > x1 || p.box[3] < y0 || p.box[1] > y1) continue;
          for (const r of p.rings) {
            ctx.moveTo((r[0] - x0) * k, (r[1] - y0) * k);
            for (let i = 2; i < r.length; i += 2) ctx.lineTo((r[i] - x0) * k, (r[i + 1] - y0) * k);
            ctx.closePath();
          }
        }
        ctx.fill("evenodd");                                 // a polygon's holes, and only its own
      }
    },
  });
  return new Layer();
}
