// Leaflet (+ markercluster), loaded the first time a map is shown. Tiles: CARTO, light or
// dark with the page. Leaflet's markers here are built as elements (never HTML strings with
// style attributes), so the page's Content-Security-Policy needs no inline styles.
import { html, useEffect, useRef, api, isDark } from "./lib.js";

let loading = null;
export function leaflet() {
  if (!loading) loading = (async () => {
    const at = (file) => new URL(`../vendor/${file}`, import.meta.url).href;   // beside this module: same version
    for (const href of [at("leaflet.css"), at("MarkerCluster.css")]) {
      const link = document.createElement("link"); link.rel = "stylesheet"; link.href = href; document.head.append(link);
    }
    for (const src of [at("leaflet.js"), at("leaflet.markercluster.js")]) {
      await new Promise((ok, fail) => {
        const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = fail; document.head.append(s);
      });
    }
    const cfg = await api("/api/map/config").catch(() => ({ carto_key: "" }));
    return { L: window.L, cfg };
  })().catch((e) => { loading = null; throw e; });
  return loading;   // {L, cfg}: cfg is /api/map/config, read once per page load
}


// A map on CARTO's tiles. labels: the place names as tiles of their own, in a pane at that z-index (the Weather
// map's, so the satellite doesn't hide them: #95), the base without them. Else the names are in the base tiles.
export function baseMap({ L, cfg }, el, { labels = false, ...options } = {}) {
  const map = L.map(el, { worldCopyJump: true, minZoom: 2, ...options });
  const key = cfg.carto_key ? `?key=${encodeURIComponent(cfg.carto_key)}` : "";
  let mode = "auto";                               // map.setBasemap("auto" | "light" | "dark"): auto follows the theme
  let labelMode = null;                            // map.setLabels("light" | "dark" | null): null, as the basemap
  const dark = () => (mode === "auto" ? isDark() : mode === "dark");
  const style = (m) => (m ? m === "dark" : dark()) ? "dark" : "light";
  const url = (kind, m) => `https://basemaps.cartocdn.com/rastertiles/${style(m)}_${kind}/{z}/{x}/{y}{r}.png${key}`;
  const tiles = L.tileLayer(url(labels ? "nolabels" : "all"), {
    maxZoom: 20,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
  }).addTo(map);
  let names = null;
  if (labels) {
    const pane = map.createPane("cblabels");
    pane.style.zIndex = labels;
    pane.style.pointerEvents = "none";
    names = L.tileLayer(url("only_labels"), { pane: "cblabels", maxZoom: 20 }).addTo(map);
  }
  map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
  const swap = () => {                             // light or dark tiles, following the theme
    tiles.setUrl(url(labels ? "nolabels" : "all"));
    names?.setUrl(url("only_labels", labelMode));
  };
  map.setBasemap = (m) => { if (m !== mode) { mode = m; swap(); } return map; };
  map.setLabels = (m) => { if (m !== labelMode) { labelMode = m; swap(); } return map; };
  window.addEventListener("cb:theme", swap);
  map.on("unload", () => window.removeEventListener("cb:theme", swap));
  return map;
}

// A small map of many photos (a trip's): a dot for each, fitted to them all. points: [[id, lat, lon, ...]].
export function PointsMap({ points, color = "#1dab45" }) {
  const el = useRef(null);
  useEffect(() => {
    let map = null, live = true;
    if (!points.length) return;
    leaflet().then((lf) => {
      if (!live || !el.current) return;
      const { L } = lf;
      map = baseMap(lf, el.current, { scrollWheelZoom: false });
      for (const [, lat, lon] of points) L.circleMarker([lat, lon], { radius: 5, color: "#fff", weight: 1, fillColor: color, fillOpacity: 0.9 }).addTo(map);
      map.fitBounds(L.latLngBounds(points.map(([, lat, lon]) => [lat, lon])), { padding: [20, 20], maxZoom: 14 });
    }).catch(() => {});
    return () => { live = false; map?.remove(); };
  }, [points]);
  return points.length ? html`<div class="pointsmap" ref=${el}></div>` : null;
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
