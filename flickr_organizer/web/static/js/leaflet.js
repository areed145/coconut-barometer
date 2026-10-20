// Leaflet (+ markercluster), loaded the first time a map is shown. Tiles: CARTO, light or
// dark with the page. Leaflet's markers here are built as elements (never HTML strings with
// style attributes), so the page's Content-Security-Policy needs no inline styles.
import { html, useEffect, useRef, api, isDark } from "./lib.js";

let loading = null;
export function leaflet() {
  if (!loading) loading = (async () => {
    for (const href of ["/static/vendor/leaflet.css", "/static/vendor/MarkerCluster.css"]) {
      const link = document.createElement("link"); link.rel = "stylesheet"; link.href = href; document.head.append(link);
    }
    for (const src of ["/static/vendor/leaflet.js", "/static/vendor/leaflet.markercluster.js"]) {
      await new Promise((ok, fail) => {
        const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = fail; document.head.append(s);
      });
    }
    const cfg = await api("/api/map/config").catch(() => ({ carto_key: "" }));
    return { L: window.L, cfg };
  })().catch((e) => { loading = null; throw e; });
  return loading;   // {L, cfg}: cfg is /api/map/config, read once per page load
}


export function baseMap({ L, cfg }, el, options = {}) {
  const map = L.map(el, { worldCopyJump: true, minZoom: 2, ...options });
  const key = cfg.carto_key ? `?key=${encodeURIComponent(cfg.carto_key)}` : "";
  const url = () => `https://basemaps.cartocdn.com/rastertiles/${isDark() ? "dark_all" : "light_all"}/{z}/{x}/{y}{r}.png${key}`;
  const tiles = L.tileLayer(url(), {
    maxZoom: 20,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
  }).addTo(map);
  map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
  const swap = () => tiles.setUrl(url());          // light or dark tiles, following the theme
  window.addEventListener("fo:theme", swap);
  map.on("unload", () => window.removeEventListener("fo:theme", swap));
  return map;
}

// A small map with one pin (the lightbox).
export function MiniMap({ lat, lon, color = "#1dab45" }) {
  const el = useRef(null);
  useEffect(() => {
    let map = null, live = true;
    leaflet().then((lf) => {
      if (!live || !el.current) return;
      const { L } = lf;
      map = baseMap(lf, el.current, { zoomControl: false, scrollWheelZoom: false }).setView([lat, lon], 12);
      L.circleMarker([lat, lon], { radius: 7, color: "#fff", weight: 2, fillColor: color, fillOpacity: 1 }).addTo(map);
    }).catch(() => {});
    return () => { live = false; map?.remove(); };
  }, [lat, lon]);
  return html`<div class="minimap" ref=${el}></div>`;
}
