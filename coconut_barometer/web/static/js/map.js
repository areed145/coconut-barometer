// The Map page: every geotagged photo, clustered. A cluster's size and color follow its
// photo count on a log scale (map.color_low → map.color_high). Clicking one with map.expand_at
// photos or fewer opens it into thumbnails; a bigger one zooms straight to where it splits up.
// A bigger one that never would (its photos all at one spot, even at the closest zoom) opens
// them in the lightbox at once.
import { html, useState, useEffect, useRef, api, num, toast } from "./lib.js";
import { leaflet, baseMap } from "./leaflet.js";
import { Lightbox } from "./gallery.js";

const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
const mix = (a, b, t) => a.map((x, i) => Math.round(x + (b[i] - x) * t));
const ink = ([r, g, b]) => (0.299 * r + 0.587 * g + 0.114 * b > 150 ? "#1b1b1b" : "#ffffff");
const short = (n) => (n >= 10000 ? Math.round(n / 1000) + "k" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n));
const RADIUS = 60;          // px: markers this close are clustered (maxClusterRadius)
const thumb = (id, server, secret) => (server && secret ? `https://live.staticflickr.com/${server}/${id}_${secret}_q.jpg` : "");

function clusterIcon(L, n, total, lo, hi) {
  const t = Math.min(1, Math.log(n) / Math.log(Math.max(3, total)));
  const size = Math.round(30 + 34 * t);
  const color = mix(lo, hi, t);
  const el = document.createElement("div");
  el.className = "mc";
  el.style.width = el.style.height = size + "px";
  el.style.background = `rgb(${color.join(",")})`;
  el.style.color = ink(color);
  el.style.fontSize = Math.round(11 + 5 * t) + "px";
  el.textContent = short(n);
  return L.divIcon({ html: el, className: "mc-wrap", iconSize: [size, size] });
}

function parseAt(at) {   // "@lat,lon,zoom" from #/photos/map/@…
  const m = /^@(-?[\d.]+),(-?[\d.]+)(?:,(\d+))?$/.exec(at || "");
  return m ? { center: [parseFloat(m[1]), parseFloat(m[2])], zoom: parseInt(m[3] || "14", 10) } : null;
}

export function MapPage({ at }) {
  const el = useRef(null);
  const map = useRef(null);
  const layer = useRef(null);
  const [ready, setReady] = useState(false);
  const [data, setData] = useState(null);
  const [albums, setAlbums] = useState([]);
  const [f, setF] = useState({ album: "", from: "", to: "" });
  const [box, setBox] = useState(null);           // {photos, index}: the lightbox
  const fit = useRef(!parseAt(at));               // fit the photos into view after loading?

  useEffect(() => { api("/api/albums").then(setAlbums).catch(() => {}); }, []);
  useEffect(() => {
    let live = true;
    leaflet().then((lf) => {
      if (!live || !el.current) return;
      const view = parseAt(at);
      map.current = baseMap(lf, el.current).setView(view ? view.center : [20, 0], view ? view.zoom : 2);
      setReady(true);
    }).catch(() => toast("Couldn't load the map."));
    return () => { live = false; map.current?.remove(); map.current = null; };
  }, []);
  useEffect(() => { const v = parseAt(at); if (v && map.current) map.current.setView(v.center, v.zoom); }, [at]);
  useEffect(() => {
    api("/api/map/points", { params: { album: f.album, year_from: f.from, year_to: f.to } })
      .then(setData).catch((e) => toast("Couldn't load the photos: " + e.message));
  }, [f.album, f.from, f.to]);

  const open = async (ids, index) => {
    try {
      const photos = await api("/api/map/cards", { method: "POST", body: { ids: ids.slice(0, 500) } });
      if (photos.length) setBox({ photos, index: Math.min(index, photos.length - 1) });
    } catch (e) { toast(e.message); }
  };

  useEffect(() => {
    const m = map.current, L = window.L;
    if (!ready || !m || !data) return;
    layer.current?.remove();
    const [lo, hi] = data.colors.map(rgb);
    const total = data.points.length;
    const group = L.markerClusterGroup({
      zoomToBoundsOnClick: false, spiderfyOnMaxZoom: false, showCoverageOnHover: false, chunkedLoading: true,
      maxClusterRadius: RADIUS, spiderfyDistanceMultiplier: 2.4,
      iconCreateFunction: (c) => clusterIcon(L, c.getChildCount(), total, lo, hi),
    });
    group.addLayers(data.points.map(([id, lat, lon, server, secret]) => {
      const mk = L.marker([lat, lon], { keyboard: false, title: "",
        icon: L.icon({ iconUrl: thumb(id, server, secret), iconSize: [40, 40], className: "mthumb" }) });
      mk.photoId = id;
      return mk;
    }));
    group.on("clusterclick", (e) => {
      const c = e.layer, n = c.getChildCount(), b = c.getBounds(), max = m.getMaxZoom();
      // How far apart its photos would be at the closest zoom: closer than a cluster's radius, it never splits.
      const apart = m.project(b.getNorthEast(), max).distanceTo(m.project(b.getSouthWest(), max));
      if (n <= data.expand_at) c.spiderfy();
      else if (apart < RADIUS || m.getZoom() >= max)
        open(c.getAllChildMarkers().map((x) => x.photoId), 0);       // all at one spot: browse them
      else c.zoomToBounds({ padding: [30, 30] });                     // straight to where it splits, not a level a click
    });
    group.on("click", (e) => {     // a thumbnail: browse it with the others it opened with
      const spread = group._spiderfied?.getAllChildMarkers();
      const ids = spread && spread.includes(e.layer) ? spread.map((x) => x.photoId) : [e.layer.photoId];
      open(ids, ids.indexOf(e.layer.photoId));
    });
    m.addLayer(group);
    layer.current = group;
    if (fit.current && total) m.fitBounds(group.getBounds(), { padding: [30, 30], maxZoom: 14 });
    fit.current = true;
  }, [ready, data]);

  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const years = data?.years || [];
  return html`
    <div class="toolbar">
      <select value=${f.album} onChange=${set("album")} aria-label="Album">
        <option value="">All albums</option>
        ${albums.map((a) => html`<option value=${a.id}>${a.title}</option>`)}
      </select>
      <select value=${f.from} onChange=${set("from")} aria-label="From year">
        <option value="">From any year</option>${years.map((y) => html`<option value=${y}>from ${y}</option>`)}
      </select>
      <select value=${f.to} onChange=${set("to")} aria-label="To year">
        <option value="">to any year</option>${years.map((y) => html`<option value=${y}>to ${y}</option>`)}
      </select>
      <span class="muted">${data ? `${num(data.points.length)} geotagged photo${data.points.length === 1 ? "" : "s"}` : "Loading…"}</span>
      <span class="spacer"></span>
      <span class="muted small hint-pointer">Click a cluster to zoom in; small ones open into thumbnails, a crowd at one spot opens its photos</span>
    </div>
    <div class="mapwrap" ref=${el}></div>
    ${box && html`<${Lightbox} photos=${box.photos} index=${box.index} onIndex=${(i) => setBox({ ...box, index: i })}
        onClose=${() => setBox(null)} />`}`;
}
