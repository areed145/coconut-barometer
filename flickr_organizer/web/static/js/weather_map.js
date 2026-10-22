// The Weather → Map tab: NOAA NEXRAD radar and GOES-East satellite over the stations, looped over the last
// hour or so, with the stations' current readings and their recent lightning. A Tempest reports how far
// away a strike was but not which way, so lightning is drawn as a ring at that distance around the station.
// Markers and popups are built as elements (no HTML strings with style attributes) to suit the page's CSP.
import { html, useState, useEffect, useRef, api, toast } from "./lib.js";
import { leaflet, baseMap } from "./leaflet.js";

const clock = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const visible = () => document.visibilityState !== "hidden";
const SATS = [["", "No satellite"], ["geocolor", "Satellite: color"], ["infrared", "Satellite: infrared"]];
// The NWS reflectivity colors, light rain (5 dBZ) to hail (70+).
const DBZ = "linear-gradient(90deg, #04e9e7, #019ff4, #0300f4, #02fd02, #01c501, #008e00, #fdf802, #e5bc00, #fd9500, #fd0000, #bc0000, #f800fd, #9854c6)";

// The newest frame at or before `t` (or the oldest, when all are later).
const frameAt = (frames, t) => frames.reduce((best, f) => (f.t <= t + 150 ? f : best), frames[0]);

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

export function WxMap({ show, unitsFor, compass }) {
  const box = useRef(null), map = useRef(null);
  const tiles = useRef({ radar: new Map(), sat: new Map() });     // frame key -> Leaflet tile layer
  const pins = useRef(new Map()), rings = useRef(null);
  const [ready, setReady] = useState(false);
  const [info, setInfo] = useState(null);          // /api/wx/map: stations and the frames available
  const [live, setLive] = useState(null);          // /api/wx/live: the readings on the pins
  const [strikes, setStrikes] = useState({});      // device -> /api/wx/lightning cells for the last hour
  const [radar, setRadar] = useState(true);
  const [sat, setSat] = useState("");
  const [showRings, setShowRings] = useState(true);
  const [opacity, setOpacity] = useState(0.7);
  const [idx, setIdx] = useState(null);            // position in the loop; null = the newest frame
  const [playing, setPlaying] = useState(false);

  useEffect(() => {
    let alive = true;
    leaflet().then((lf) => {
      if (!alive || !box.current) return;
      map.current = baseMap(lf, box.current, { minZoom: 3 }).setView([39, -96], 4);
      map.current.createPane("wxsat").style.zIndex = 350;      // above the base map, under the radar and markers
      map.current.createPane("wxradar").style.zIndex = 360;
      rings.current = lf.L.layerGroup().addTo(map.current);
      setReady(true);
    }).catch(() => toast("Couldn't load the map."));
    return () => { alive = false; map.current?.remove(); map.current = null; };
  }, []);

  // Frames every 5 minutes and readings every 30 seconds, while the tab is visible.
  useEffect(() => {
    let alive = true;
    const frames = () => visible() && api("/api/wx/map").then((x) => alive && setInfo(x)).catch((e) => alive && toast(e.message));
    const readings = () => visible() && api("/api/wx/live").then((x) => alive && setLive(x)).catch(() => {});
    frames(); readings();
    const a = setInterval(frames, 300000), b = setInterval(readings, 30000);
    return () => { alive = false; clearInterval(a); clearInterval(b); };
  }, []);
  useEffect(() => {
    if (!info) return;
    let alive = true;
    for (const s of info.stations) api(`/api/wx/lightning?device=${s.device_id}&hours=1`)
      .then((x) => alive && setStrikes((m) => ({ ...m, [s.device_id]: x.cells }))).catch(() => {});
    return () => { alive = false; };
  }, [info]);

  // Fit the stations into view the first time they're known.
  const fitted = useRef(false);
  useEffect(() => {
    const m = map.current;
    if (!ready || !m || !info || fitted.current || !info.stations.length) return;
    fitted.current = true;
    const pts = info.stations.map((s) => [s.lat, s.lon]);
    if (pts.length === 1) m.setView(pts[0], 7); else m.fitBounds(pts, { padding: [80, 80], maxZoom: 8 });
  }, [ready, info]);

  // One tile layer per frame, all kept on the map so the loop plays from cache; only the current one shows.
  const radarFrames = info?.radar.frames || [], satInfo = sat ? info?.satellite[sat] : null, satFrames = satInfo?.frames || [];
  useEffect(() => {
    const m = map.current, L = window.L;
    if (!ready || !m || !info) return;
    const sync = (store, want, make) => {
      for (const [k, layer] of store) if (!want.has(k)) { layer.remove(); store.delete(k); }
      for (const k of want.keys()) if (!store.has(k)) store.set(k, make(want.get(k)).setOpacity(0).addTo(m));
    };
    sync(tiles.current.radar, new Map(radar ? radarFrames.map((f) => [f.layer, f]) : []), (f) =>
      L.tileLayer(info.radar.url.replace("{layer}", f.layer), { pane: "wxradar", maxZoom: 20,
        attribution: 'Radar: <a href="https://mesonet.agron.iastate.edu/">IEM</a> · NWS NEXRAD' }));
    sync(tiles.current.sat, new Map(satFrames.map((f) => [`${sat}:${f.time}`, f])), (f) =>
      L.tileLayer(satInfo.url.replace("{time}", f.time), { pane: "wxsat", maxZoom: 20, maxNativeZoom: satInfo.max_zoom,
        attribution: 'Satellite: <a href="https://earthdata.nasa.gov/gibs">NASA GIBS</a> · NOAA GOES-East' }));
  }, [ready, info, radar, sat]);

  // The loop runs over the radar's 5-minute frames, or the satellite's 10-minute ones when radar is off.
  const timeline = radar && radarFrames.length ? radarFrames : satFrames;
  const at = timeline.length ? Math.min(idx ?? timeline.length - 1, timeline.length - 1) : -1;
  const t = at >= 0 ? timeline[at].t : null;
  const shownRadar = radar && radarFrames.length && t != null ? frameAt(radarFrames, t) : null;
  const shownSat = satFrames.length && t != null ? frameAt(satFrames, t) : null;
  useEffect(() => {
    for (const [k, layer] of tiles.current.radar) layer.setOpacity(shownRadar && k === shownRadar.layer ? opacity : 0);
    for (const [k, layer] of tiles.current.sat) layer.setOpacity(shownSat && k === `${sat}:${shownSat.time}` ? Math.min(1, opacity + 0.2) : 0);
  });

  useEffect(() => {
    if (!playing || !timeline.length) return;
    let hold = 0;
    const tick = setInterval(() => setIdx((i) => {
      const n = timeline.length, cur = i ?? n - 1;
      if (cur < n - 1) return cur + 1;
      if (hold++ < 3) return cur;                  // linger on the newest frame before starting over
      hold = 0;
      return 0;
    }), 600);
    return () => clearInterval(tick);
  }, [playing, timeline.length]);

  // Station pins: the temperature, an arrow the way the wind blows, and a popup of the main readings.
  useEffect(() => {
    const m = map.current, L = window.L;
    if (!ready || !m || !info) return;
    const unit = live?.units || info.units;
    for (const s of info.stations) {
      let pin = pins.current.get(s.device_id);
      if (!pin) {
        const face = el("div", "wxpin"), temp = el("b"), arrow = el("i");
        face.append(temp, arrow);
        const pop = el("div", "wxpop");
        const marker = L.marker([s.lat, s.lon], { icon: L.divIcon({ html: face, className: "wxpin-wrap", iconSize: [56, 28], iconAnchor: [28, 14] }) })
          .bindPopup(pop).addTo(m);
        pin = { marker, temp, arrow, pop };
        pins.current.set(s.device_id, pin);
      }
      const o = live?.stations.find((x) => x.device_id === s.device_id)?.obs;
      pin.temp.textContent = o?.temp == null ? "–" : `${Math.round(unitsFor(unit).temp[1](o.temp))}°`;
      pin.arrow.hidden = o?.wind_dir == null || !o.wind_avg;
      if (o?.wind_dir != null) pin.arrow.style.transform = `rotate(${(o.wind_dir + 180) % 360}deg)`;
      const lines = !o ? ["No readings yet."] : [
        `${show(unit, "temp", o.temp)} · feels ${show(unit, "temp", o.feels_like, 0)}`,
        `Wind ${show(unit, "speed", o.wind_avg)} ${compass(o.wind_dir)} · gust ${show(unit, "speed", o.wind_gust, 0)}`,
        `Humidity ${o.humidity == null ? "–" : Math.round(o.humidity)}% · ${show(unit, "pressure", o.pressure_sl, unit === "imperial" ? 2 : 1)}`,
        `Rain today ${show(unit, "rain", o.rain_today, unit === "imperial" ? 2 : 1)}`];
      const link = el("a", null, "Open Live →");
      link.href = "#/weather/live";
      pin.pop.replaceChildren(el("b", null, s.name), ...lines.map((l) => el("div", null, l)), link);
    }
  }, [ready, info, live]);

  // Lightning in the last hour: a ring per station and distance, fading with age.
  useEffect(() => {
    const L = window.L, group = rings.current;
    if (!ready || !group || !info) return;
    group.clearLayers();
    if (!showRings) return;
    const unit = live?.units || info.units, now = Date.now() / 1000, color = css("--chart-2");
    for (const s of info.stations) {
      const byKm = new Map();
      for (const [ts, km, n] of strikes[s.device_id] || []) {
        const r = byKm.get(km) || { ts: 0, n: 0 };
        byKm.set(km, { ts: Math.max(r.ts, ts), n: r.n + n });
      }
      for (const [km, r] of byKm) {
        const fresh = Math.max(0.15, 1 - (now - r.ts) / 3600);
        L.circle([s.lat, s.lon], { radius: Math.max(500, km * 1000), color, weight: 1 + Math.min(3, r.n / 4), opacity: fresh, fill: false, interactive: true })
          .bindTooltip(`${s.name}: ${r.n} strike${r.n === 1 ? "" : "s"} ${show(unit, "dist", km, 0)} away, last at ${clock(r.ts)}`, { sticky: true })
          .addTo(group);
      }
    }
  }, [ready, info, live, strikes, showRings]);

  const total = info ? info.stations.reduce((a, s) => a + (strikes[s.device_id] || []).reduce((b, c) => b + c[2], 0), 0) : 0;
  return html`
    <div class="toolbar wxmapbar">
      <button class=${radar ? "primary" : ""} onClick=${() => { setRadar(!radar); setIdx(null); }}>Radar</button>
      <span class="seg" aria-label="Satellite">${SATS.map(([k, l]) => html`<button key=${k} class=${k === sat ? "primary" : ""}
        onClick=${() => { setSat(k); setIdx(null); }}>${l}</button>`)}</span>
      <button class=${showRings ? "primary" : ""} onClick=${() => setShowRings(!showRings)}>Lightning${total ? ` · ${total}` : ""}</button>
      <span class="wxloop">
        <button disabled=${timeline.length < 2} onClick=${() => setPlaying(!playing)} aria-label=${playing ? "Pause" : "Play"}>${playing ? "❚❚" : "▶"}</button>
        <input type="range" aria-label="Frame" min="0" max=${Math.max(0, timeline.length - 1)} value=${Math.max(0, at)} disabled=${timeline.length < 2}
          onInput=${(e) => { setPlaying(false); setIdx(Number(e.target.value)); }} />
        <span class="muted">${[shownRadar && `radar ${clock(shownRadar.t)}`, shownSat && `satellite ${clock(shownSat.t)}`].filter(Boolean).join(" · ") || "no imagery"}</span>
      </span>
      <label class="muted wxopacity">Opacity <input type="range" min="0.2" max="1" step="0.05" value=${opacity} onInput=${(e) => setOpacity(Number(e.target.value))} /></label>
    </div>
    <div class="mapwrap" ref=${box}></div>
    <p class="muted heatkey">${radar && html`<span>radar: light</span><i style=${`background: ${DBZ}`}></i><span>heavy / hail</span>`}
      <span>${showRings ? "Rings: lightning in the last hour, at the distance the station measured (it can't tell direction)." : ""}
        ${info?.radar.error ? " Radar unavailable right now." : ""}${satInfo?.error ? " Satellite unavailable right now." : ""}
        ${sat ? " Satellite images run about 30–60 minutes behind." : ""}</span></p>`;
}
