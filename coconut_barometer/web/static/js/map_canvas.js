// One canvas over the map in a pane, moved and scaled with it the way Leaflet's own vector renderer is, so the radar
// and the fronts stay on screen while the map moves: the canvas covers the view and PAD of it more on every side (a
// pan shows what's already drawn), is scaled along with a zoom (its animation, or a pinch) instead of hidden, and is
// drawn again once the move ends. A layer extends it with _paint(ctx, corner, size, zoom): the canvas's top left in
// world pixels at `zoom` (map.project's), its size in CSS pixels, and ctx already scaled to CSS pixels. Both are as the
// canvas was laid out, not the map now, so whatever is drawn mid-zoom still lines up.

import { IOS } from "./device.js";

export const PAD = 0.3;
// A big screen's padded canvas at 2x would be tens of megapixels: drawn coarser instead. iOS and iPadOS close a tab
// that holds too much (#102), and the map has up to eight of these at once, so there they're half that and drawn at
// no more than 2x (a 3x phone's radar, blurred anyway, looks the same).
const MAX_PIXELS = IOS ? 4e6 : 8e6;
const MAX_RATIO = IOS ? 2 : Infinity;

export function mapCanvas(L, className) {
  return {
    onAdd() {
      this._canvas = L.DomUtil.create("canvas", `${className} leaflet-zoom-animated`, this.getPane());
      if (this._opacity != null) this._canvas.style.opacity = this._opacity;
      this._reset();
    },
    onRemove() {
      this._canvas.width = this._canvas.height = 0;   // its memory back now: WebKit holds a dropped canvas's until GC
      L.DomUtil.remove(this._canvas);
      this._canvas = null;
    },
    getEvents() {
      const events = { viewreset: this._reset, moveend: this._reset, resize: this._reset, zoom: this._onZoom };
      if (this._map._zoomAnimated) events.zoomanim = this._onAnimZoom;
      return events;
    },
    _onZoom() { this._transform(this._map.getCenter(), this._map.getZoom()); },
    _onAnimZoom(e) { this._transform(e.center, e.zoom); },
    // Leaflet's Renderer._updateTransform: where the canvas drawn at this._zoom goes, and how big, at `zoom`.
    _transform(center, zoom) {
      const map = this._map, scale = map.getZoomScale(zoom, this._zoom);
      const half = this._size.multiplyBy(0.5);
      const offset = half.multiplyBy(-scale).add(map.project(this._center, zoom)).subtract(map._getNewPixelOrigin(center, zoom));
      L.DomUtil.setTransform(this._canvas, offset, scale);
    },
    _reset() {
      const map = this._map, c = this._canvas;
      if (!c) return;
      const view = map.getSize(), size = view.multiplyBy(1 + 2 * PAD).round();
      this._center = map.getCenter();
      this._zoom = map.getZoom();
      this._size = size;
      this._origin = map.containerPointToLayerPoint(view.subtract(size).divideBy(2)).round();
      this._corner = this._origin.add(map.getPixelOrigin());
      this._ratio = Math.min(window.devicePixelRatio || 1, MAX_RATIO, Math.sqrt(MAX_PIXELS / (size.x * size.y)));
      L.DomUtil.setPosition(c, this._origin);                  // also drops the zoom's scale
      c.width = Math.round(size.x * this._ratio);
      c.height = Math.round(size.y * this._ratio);
      c.style.width = `${size.x}px`;
      c.style.height = `${size.y}px`;
      this._draw();
    },
    _draw() {
      const c = this._canvas;
      if (!c || !this._size) return;
      const ctx = c.getContext("2d");
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.filter = "none";
      ctx.clearRect(0, 0, c.width, c.height);
      ctx.setTransform(this._ratio, 0, 0, this._ratio, 0, 0);
      this._paint(ctx, this._corner, this._size, this._zoom);
    },
  };
}
