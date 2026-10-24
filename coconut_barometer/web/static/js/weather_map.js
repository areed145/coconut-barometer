// The Weather → Map tab: radar and GOES-East satellite over the stations, with the stations' current readings
// and their recent lightning. Radar (NOAA MRMS as shapes per dBZ level, in Settings → Display → Radar colors) and
// storm tracks (the cells the NWS radars track, and where they're heading) are layers of their own. Radar, storm
// tracks and satellite share one timeline over the last hour or so, opening on now; the radar's past frames load
// only once it's played or scrubbed. The shapes come for the view: ~2 km detail and only what's in sight when zoomed
// in, ~8 km for the US zoomed out (radar_layer.js draws them). Fronts and highs and lows are WPC's latest surface
// analysis (fronts_layer.js), whatever the timeline shows. The map's own errors go to the server (#65), which
// reports them like its own. A Tempest reports how far
// away a strike was but not which way, so lightning is drawn as a ring at that distance around the station.
// Markers and popups are built as elements (no HTML strings with style attributes) to suit the page's CSP.
// The controls (wx_controls.js, #78) sit on the map: my location and full screen with the zoom buttons, the Layers
// panel (a bottom sheet on a phone) and the timeline bar along the bottom; the layer choices are remembered.
import { html, useState, useEffect, useRef, api, toast, every } from "./lib.js";
import { leaflet, baseMap } from "./leaflet.js";
import { decode, radarCanvas, levelColor, radarScale, DEFAULT_STOPS } from "./radar_layer.js";
import { frontsCanvas } from "./fronts_layer.js";
import { loopWindow } from "./wx_loop.js";
import { toolsControl, useLocate, useFullscreen, LayersPanel, LoopBar, SPEEDS, loadLayers, saveLayers } from "./wx_controls.js";

const clock = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];

const FINE_ZOOM = 6;          // radar.py FINE_ZOOM: from here in, the ~2 km shapes for the view
const LOADING = 4;            // loop frames fetched at once
// What the radar shapes are asked for: zoomed in, the view padded by half its size and rounded out to whole degrees
// (so small pans need nothing new); zoomed out, the whole US.
function radarView(m) {
  const zoom = m.getZoom();
  if (zoom < FINE_ZOOM) return { key: "us", zoom, bbox: null };
  const b = m.getBounds().pad(0.5);
  const bbox = [Math.floor(b.getWest()), Math.floor(b.getSouth()), Math.ceil(b.getEast()), Math.ceil(b.getNorth())]
    .map((v, i) => Math.max(i % 2 ? -90 : -180, Math.min(i % 2 ? 90 : 180, v)));
  return { key: bbox.join(","), zoom, bbox };
}
const inside = (view, m) => {                    // is the map still within what `view` fetched?
  if (!view || (m.getZoom() >= FINE_ZOOM) !== (view.zoom >= FINE_ZOOM)) return false;
  if (!view.bbox) return true;
  const b = m.getBounds();
  return b.getWest() >= view.bbox[0] && b.getSouth() >= view.bbox[1] && b.getEast() <= view.bbox[2] && b.getNorth() <= view.bbox[3];
};
const radarQuery = (view) => `zoom=${view.zoom}` + (view.bbox ? `&bbox=${view.bbox.join(",")}` : "");
const unpack = (x) => ({ ...x, shapes: decode(x.shapes) });

// The newest time in `times` (ascending) at or before `t`, or null.
const latestBy = (times, t) => { let best = null; for (const x of times) if (x <= t) best = x; else break; return best; };

// An error in the map's own code (not an extension's, nor a failed API call), sent to the server once per message. The server logs it, and reports it to GitHub when it comes from a signed-in browser.
const reported = new Set();
function reportError(source, error) {
  try {
    const e = error instanceof Error ? error : new Error(String(error ?? "unknown error"));
    const stack = String(e.stack || "");
    // An ApiError (it has a status) is the server's to report, or the network being down: not the map's.
    if (!stack.includes(location.origin + "/static/") || e.status !== undefined || reported.has(e.message)) return;
    reported.add(e.message);
    fetch("/api/wx/map/errors", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, name: String(e.name || "Error").slice(0, 100), message: String(e.message || "error").slice(0, 500),
        stack: stack.slice(0, 6000) }) }).catch(() => {});
  } catch { /* reporting must never be the next error */ }
}

// The newest frame at or before `t` (or the oldest, when all are later).
const frameAt = (frames, t) => frames.reduce((best, f) => (f.t <= t + 150 ? f : best), frames[0]);

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

export function WxMap({ show, unitsFor, compass }) {
  const wrap = useRef(null), box = useRef(null), map = useRef(null), tools = useRef(null), handlers = useRef({});
  const tiles = useRef({ sat: new Map() });     // satellite frame key -> Leaflet tile layer
  const pins = useRef(new Map()), rings = useRef(null), canvas = useRef(null), storms = useRef(null), frontsLayer = useRef(null);
  const [ready, setReady] = useState(false);
  const [info, setInfo] = useState(null);          // /api/wx/map: stations and the frames available
  const [live, setLive] = useState(null);          // /api/wx/live: the readings on the pins
  const [strikes, setStrikes] = useState({});      // device -> /api/wx/lightning cells for the last hour
  const [layers, setLayers] = useState(loadLayers);    // the Layers panel's choices, remembered on this device
  const set = (change) => setLayers((l) => ({ ...l, ...change }));
  useEffect(() => saveLayers(layers), [layers]);
  const { radar: showRadar, storms: showStorms, fronts: showFronts, sat, lightning: showRings, radarOpacity: opacity } = layers;
  const [fronts, setFronts] = useState(null);      // /api/wx/fronts: WPC's latest surface analysis
  const [view, setView] = useState(null);          // what the radar shapes are fetched for (radarView)
  const [live_, setLiveFrame] = useState(null);     // /api/wx/radar for the view: the newest shapes and the storm cells
  const [times, setTimes] = useState([]);          // /api/wx/radar/frames: the loop's kept frames
  const kept = useRef(new Map());                  // the loop's frames loaded for the view: t -> unpacked frame
  const gone = useRef(new Set());                  // listed frames the server can't give (404): left out of the loop
  const [, setLoaded] = useState(0);               // bumped as they arrive
  const [idx, setIdx] = useState(null);            // position in the loop; null = the newest frame
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);           // SPEEDS: ½×, 1×, 2×
  const locate = useLocate(map, ready);
  const fullscreen = useFullscreen(wrap, map);
  handlers.current = { locate: locate.tap, full: fullscreen.toggle };

  useEffect(() => {
    let alive = true;
    const onError = (e) => reportError("error", e.error || e.message);
    const onRejection = (e) => reportError("unhandledrejection", e.reason);
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    leaflet().then((lf) => {
      if (!alive || !box.current) return;
      map.current = baseMap(lf, box.current, { minZoom: 3 }).setView([39, -96], 4);
      map.current.createPane("wxsat").style.zIndex = 350;      // above the base map, under the radar and markers
      map.current.createPane("wxradar").style.zIndex = 360;
      map.current.createPane("wxfronts").style.zIndex = 370;     // over the radar, under the storm cells and pins
      map.current.createPane("wxme").style.zIndex = 650;         // my location: over the pins, under the popups
      tools.current = toolsControl(lf.L, handlers).addTo(map.current);
      rings.current = lf.L.layerGroup().addTo(map.current);
      storms.current = lf.L.layerGroup().addTo(map.current);
      canvas.current = radarCanvas(lf.L, "wxradar", 'Radar: <a href="https://mrms.nssl.noaa.gov/">NOAA MRMS</a>').addTo(map.current);
      frontsLayer.current = frontsCanvas(lf.L, "wxfronts", 'Fronts: <a href="https://www.wpc.ncep.noaa.gov/html/sfctxt.html">NOAA WPC</a>');
      const moved = () => setView((v) => (inside(v, map.current) ? v : radarView(map.current)));
      map.current.on("moveend", moved);
      moved();
      setReady(true);
    }).catch((e) => { if (alive) { toast("Couldn't load the map."); reportError("error", e); } });
    return () => {
      alive = false;
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
      map.current?.remove();
      map.current = null;
    };
  }, []);

  // Frames every 5 minutes and readings every 30 seconds, while the tab is visible.
  useEffect(() => {
    let alive = true;
    const frames = () => api("/api/wx/map").then((x) => alive && setInfo(x)).catch((e) => alive && toast(e.message));
    const readings = () => api("/api/wx/live").then((x) => alive && setLive(x)).catch(() => {});
    const loopTimes = () => api("/api/wx/radar/frames").then((x) => alive && setTimes(x.frames.map((f) => f.t))).catch(() => {});
    const stopFrames = every(frames, 300000), stopReadings = every(readings, 30000), stopTimes = every(loopTimes, 120000);
    return () => { alive = false; stopFrames(); stopReadings(); stopTimes(); };
  }, []);
  // The newest radar for the view: every 2 minutes, and whenever the view needs other shapes.
  useEffect(() => {
    if (!view) return;
    let alive = true;
    const smooth = () => api(`/api/wx/radar?${radarQuery(view)}`).then((x) => alive && setLiveFrame(unpack(x))).catch(() => {});
    const stop = every(smooth, 120000);
    return () => { alive = false; stop(); };
  }, [view?.key]);
  // Fronts while they're shown: the analysis is every 3 hours, so a look every 15 minutes is plenty.
  useEffect(() => {
    if (!showFronts) return;
    let alive = true;
    const stop = every(() => api("/api/wx/fronts").then((x) => alive && setFronts(x)).catch(() => {}), 15 * 60000);
    return () => { alive = false; stop(); };
  }, [showFronts]);
  useEffect(() => {
    const m = map.current, layer = frontsLayer.current;
    if (!ready || !m || !layer) return;
    if (!showFronts) { layer.remove(); return; }
    if (!m.hasLayer(layer)) layer.addTo(m);
    layer.set(fronts, css("--bg") || "#fff");
  }, [ready, showFronts, fronts]);
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

  // One satellite tile layer per frame, all kept on the map so the loop plays from cache; only the current one shows.
  const radarOn = showRadar || showStorms;
  const satInfo = sat ? info?.satellite[sat] : null, satFrames = satInfo?.frames || [];
  useEffect(() => {
    const m = map.current, L = window.L;
    if (!ready || !m || !info) return;
    const sync = (store, want, make) => {
      for (const [k, layer] of store) if (!want.has(k)) { layer.remove(); store.delete(k); }
      for (const k of want.keys()) if (!store.has(k)) store.set(k, make(want.get(k)).setOpacity(0).addTo(m));
    };
    sync(tiles.current.sat, satInfo?.url ? new Map(satFrames.map((f) => [`${sat}:${f.time}`, f])) : new Map(), (f) =>
      L.tileLayer(satInfo.url.replace("{time}", f.time), { pane: "wxsat", maxZoom: 20, maxNativeZoom: satInfo.max_zoom,
        attribution: 'Satellite: <a href="https://earthdata.nasa.gov/gibs">NASA GIBS</a> · NOAA GOES-East' }));
  }, [ready, info, sat]);

  // One timeline for every layer shown (wx_loop.js): with radar and satellite, the hour ending at the newest satellite
  // frame; radar alone, its last hour; satellite alone, all its frames. Each layer shows its newest frame at or before
  // the time picked (or its first, never nothing). idx null is now: the live radar and the newest satellite frame,
  // so the radar's past is only fetched once the loop is played or scrubbed.
  const loopWin = loopWindow(radarOn ? times.filter((t) => !gone.current.has(t)) : [], satFrames.map((f) => f.t));
  const timeline = loopWin.times, radarTimes = loopWin.radar;
  const at = timeline.length ? Math.min(idx ?? timeline.length - 1, timeline.length - 1) : -1;
  const t = at >= 0 ? timeline[at] : null;
  const past = radarOn && (playing || idx != null);
  const radarAt = (when) => (when == null ? null : latestBy(radarTimes, when) ?? radarTimes[0] ?? null);
  const frameOf = (rt) => (rt == null ? null : kept.current.get(rt) || (live_?.t === rt ? live_ : null));
  const shownRadar = past ? radarAt(t) : null;
  const vector = !radarOn ? null : past ? frameOf(shownRadar) : live_;
  // The radar colors: as the newest radar came with them, else the last ones known, else the defaults (#75).
  const lastColors = useRef(DEFAULT_STOPS);
  if (live_?.colors?.length) lastColors.current = live_.colors;
  const colors = lastColors.current;
  const shownSat = satFrames.length && t != null ? frameAt(satFrames, t) : null;
  useEffect(() => {
    for (const [k, layer] of tiles.current.sat) layer.setOpacity(shownSat && k === `${sat}:${shownSat.time}` ? layers.satOpacity : 0);
  });

  // The loop's frames for the view, newest first, LOADING at a time, each decoded once. Another view starts over;
  // frames gone from the server's list are dropped.
  useEffect(() => {
    if (!past || !view) return;
    let alive = true;
    const have = kept.current;
    if (have.view !== view.key) { have.clear(); have.view = view.key; have.loading = new Set(); setLoaded((n) => n + 1); }
    for (const t of have.keys()) if (!times.includes(t)) have.delete(t);
    for (const t of gone.current) if (!times.includes(t)) gone.current.delete(t);
    const todo = [...radarTimes].reverse().filter((t) => !have.has(t) && !have.loading.has(t));
    const next = () => {
      const t = todo.shift();
      if (t == null || !alive) return;
      const loading = have.loading;
      loading.add(t);
      api(`/api/wx/radar/frame?t=${t}&${radarQuery(view)}`)       // kept even if the list moved on meanwhile
        .then((x) => { if (have.view === view.key) { have.set(t, unpack(x)); setLoaded((n) => n + 1); } })
        .catch((e) => { if (e.status === 404) { gone.current.add(t); setLoaded((n) => n + 1); } })   // else tried again later
        .finally(() => { loading.delete(t); next(); });
    };
    for (let i = 0; i < LOADING; i++) next();
    return () => { alive = false; };
  }, [past, view?.key, times, radarTimes.join(",")]);

  // Whether frame k of the loop can be shown yet: its radar frame loaded, its satellite tiles in.
  const readyAt = useRef(() => true);
  readyAt.current = (k) => {
    const when = timeline[k];
    if (when == null) return false;
    const rt = radarAt(when);
    if (rt != null && !frameOf(rt)) return false;
    const s = satFrames.length ? frameAt(satFrames, when) : null;
    const layer = s && tiles.current.sat.get(`${sat}:${s.time}`);
    return !layer?.isLoading?.();
  };

  useEffect(() => {
    if (!playing || !timeline.length) return;
    let hold = 0;
    const tick = setInterval(() => setIdx((i) => {
      const n = timeline.length, cur = i ?? n - 1;
      let next = cur + 1;
      if (cur >= n - 1) {
        if (hold++ < 3) return cur;                // linger on the newest frame before starting over
        hold = 0;
        next = 0;
      }
      return readyAt.current(next) ? next : cur;   // wait for a frame still loading rather than show it empty
    }), SPEEDS[speed][1]);
    return () => clearInterval(tick);
  }, [playing, speed, timeline.length, timeline[0]]);

  // The loop pauses while the tab is hidden.
  useEffect(() => {
    const vis = () => { if (document.hidden) setPlaying(false); };
    document.addEventListener("visibilitychange", vis);
    return () => document.removeEventListener("visibilitychange", vis);
  }, []);
  const step = (d) => { setPlaying(false); setIdx((i) => Math.max(0, Math.min(timeline.length - 1, (i ?? timeline.length - 1) + d))); };
  const now = () => { setPlaying(false); setIdx(null); };
  const keys = useRef(null);
  keys.current = (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey || e.defaultPrevented) return;
    const t = e.target, range = t?.type === "range";
    if (t?.closest?.("textarea, select, [contenteditable], .wxlayers") || (t?.tagName === "INPUT" && !range)) return;
    if (e.key === " " && !t?.closest?.("button")) { e.preventDefault(); if (timeline.length > 1) setPlaying((p) => !p); }
    else if (range) return;                        // the scrubber's own arrows, Home and End
    else if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); step(e.key === "ArrowLeft" ? -1 : 1); }
    else if (e.key === "Home") { e.preventDefault(); setPlaying(false); setIdx(0); }
    else if (e.key === "End") { e.preventDefault(); now(); }
  };
  useEffect(() => {
    const on = (e) => keys.current(e);
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  // The basemap (Auto follows the theme), and the location and full-screen buttons' state.
  useEffect(() => { if (ready) map.current?.setBasemap(layers.basemap); }, [ready, layers.basemap]);
  useEffect(() => {
    const t = tools.current;
    if (!ready || !t?.locate) return;
    t.locate.className = "wxlocate " + locate.mode;
    t.locate.setAttribute("aria-pressed", String(locate.mode === "follow"));
    const label = { off: "Show my location", finding: "Finding your location…", on: "Follow my location", follow: "Stop following" }[locate.mode];
    t.locate.title = label;
    t.locate.setAttribute("aria-label", label);
    t.full.textContent = fullscreen.full ? "✕" : "⛶";
    t.full.title = fullscreen.full ? "Leave full screen" : "Full screen";
    t.full.setAttribute("aria-label", t.full.title);
  }, [ready, locate.mode, fullscreen.full]);

  // The smooth radar: the shown frame's shapes, each level colored at its middle (radar_layer.js draws them).
  useEffect(() => {
    if (!ready || !canvas.current) return;
    const shapes = showRadar && colors ? vector?.shapes : null;
    canvas.current.set(shapes, (dbz) => levelColor(colors, dbz)).setOpacity(opacity);
  }, [ready, vector, colors, showRadar, opacity]);

  // Storm cells: a dot colored by strength, and a dashed line to where it'll be in 30 and 60 minutes.
  useEffect(() => {
    const L = window.L, group = storms.current;
    if (!ready || !group) return;
    group.clearLayers();
    if (!showStorms || !vector || !colors) return;
    const unit = live_.units;
    for (const c of vector.cells) {
      const color = levelColor(colors, c.dbz) ?? css("--muted");     // its level's color; unknown: neutral
      const moving = c.kmh ? `moving ${COMPASS[Math.round(c.toward / 45) % 8]} at ${show(unit, "speed", c.kmh / 3.6, 0)}` : "barely moving";
      const tip = [`${c.dbz} dBZ storm, ${moving}`, c.posh >= 30 && `hail likely (${c.posh}% severe)`, c.tvs && "tornado vortex signature",
        c.kmh && "dashes: 30 and 60 minutes on its present course", c.valid && `scanned ${clock(c.valid)}`].filter(Boolean).join(" · ");
      // Faded as the radar is, so a storm and the level under it look the same color (#75).
      if (c.kmh) L.polyline(c.path, { color, weight: 2, dashArray: "4 6", opacity, interactive: false }).addTo(group);
      for (const p of c.kmh ? c.path.slice(1) : []) L.circleMarker(p, { radius: 2, color, weight: 1, opacity, fillOpacity: opacity, interactive: false }).addTo(group);
      L.circleMarker(c.path[0], { radius: c.tvs || c.posh >= 30 ? 8 : 6, color: css("--fg"), weight: c.tvs ? 3 : 1, fillColor: color, fillOpacity: opacity })
        .bindTooltip(tip, { sticky: true }).addTo(group);
    }
  }, [ready, vector, colors, showStorms, live, opacity]);

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
  const frontsAt = showFronts && fronts?.valid && `fronts ${new Date(fronts.valid * 1000).getUTCHours().toString().padStart(2, "0")}Z`;
  const detail = [radarOn && vector?.t && `radar ${clock(vector.t)}`, shownSat && `satellite ${clock(shownSat.t)}`, frontsAt]
    .filter(Boolean).join(" · ") || "no imagery";
  const gap = sat && loopWin.end ? Date.now() / 1000 - loopWin.end : 0;
  const legend = showRadar && colors && html`<p class="muted heatkey"><span>radar: light</span>
    <i style=${`background: ${radarScale(colors)}; opacity: ${opacity}`}></i><span>heavy / hail</span></p>`;
  const notes = html`${showRings ? "Rings: lightning in the last hour, at the distance the station measured (it can't tell direction)." : ""}
    ${radarOn && live_ && !live_.t ? " The radar starts within a few minutes of the server starting." : ""}
    ${showRadar && live_?.t ? " Radar: NOAA MRMS, cleaned." : ""}${showStorms && live_?.t ? " Dots: storms the NWS radars are tracking." : ""}
    ${radarOn && live_?.t && !times.length ? " The radar's past fills in as frames arrive (one every 2 minutes)." : ""}${satInfo?.error ? " Satellite unavailable right now." : ""}
    ${sat ? " Satellite images run about 30–60 minutes behind." : ""}
    ${showFronts ? (fronts?.valid ? ` Fronts, highs and lows: WPC's surface analysis at ${clock(fronts.valid)} (every 3 hours), whatever time the radar shows.`
      : fronts?.error ? " Fronts unavailable right now." : "") : ""}`;
  return html`
    <div ref=${wrap} class=${"wxmap" + (fullscreen.full ? " full " + fullscreen.full : "")}>
      <div class="mapwrap" ref=${box}></div>
      <${LayersPanel} layers=${layers} set=${(c) => { if ("sat" in c) setIdx(null); set(c); }} strikes=${total} legend=${legend} notes=${notes} />
      <${LoopBar} timeline=${timeline} at=${at} live=${idx == null && !playing} time=${past ? t : vector?.t ?? shownSat?.t}
        detail=${detail} playing=${playing} speed=${speed} gap=${gap}
        loaded=${(k) => (past ? readyAt.current(k) : k === timeline.length - 1)}
        onPlay=${() => setPlaying(!playing)} onStep=${step} onSeek=${(k) => { setPlaying(false); setIdx(k); }} onNow=${now}
        onSpeed=${() => setSpeed((v) => (v + 1) % SPEEDS.length)} />
      ${satInfo?.error && html`<p class="wxwarn">Satellite unavailable right now.</p>`}
    </div>`;
}
