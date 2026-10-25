// The Weather → Map tab: radar and GOES-East satellite over the stations, with the stations' current readings
// and their recent lightning. Radar (NOAA MRMS as shapes per dBZ level, in Settings → Display → Radar colors) and
// storm tracks (the cells the NWS radars track, and where they're heading) are layers of their own. Radar, storm
// tracks and satellite share one timeline over the last hour or so, opening on now; the radar's past frames load
// only once it's played or scrubbed. The shapes come for the view: ~2 km detail and only what's in sight when zoomed
// in, ~8 km for the US zoomed out (radar_layer.js draws them). Fronts and highs and lows are WPC's latest surface
// analysis (fronts_layer.js), and watches and warnings the NWS's in effect now (hazards_layer.js, #87), whatever the
// timeline shows, as are the temperature and surface wind (NOAA's RTMA analysis every 15 minutes, surface_layer.js,
// #85, #86): each fetched while it's shown (useFeed). Lightning is where GOES-East's lightning mapper saw it
// (lightning_layer.js, #94): the 15 minutes up to the time the timeline shows, fetched each minute and only what's
// new; by default as smooth shapes the server traces from them as the radar's are (weather/glm.py, #101), drawn by the
// radar's canvas, with the newest flashes as dots on top. The map's own errors go to the server (#65), which reports
// them like its own. The stations' own strikes are
// rings: a Tempest reports how far away a strike was but not which way, so each is a ring at that distance.
// The place names are the basemap's own, as tiles of their own (leaflet.js baseMap's labels) over the satellite and
// under the radar (which is sparse, and matters more); with the satellite on they turn to light text on a dark halo,
// and Natural Earth's coastlines and borders are drawn under them over the imagery (lines_layer.js, #95), which hides
// the basemap's own.
// Markers and popups are built as elements (no HTML strings with style attributes) to suit the page's CSP.
// The controls (wx_controls.js, #78) sit on the map: my location and full screen with the zoom buttons, the Layers
// panel (a bottom sheet on a phone) and the timeline bar along the bottom; the layer choices are remembered.
import { html, useState, useEffect, useRef, useMemo, api, toast, every } from "./lib.js";
import { leaflet, baseMap } from "./leaflet.js";
import { decode, radarCanvas, levelColor, radarScale, DEFAULT_STOPS } from "./radar_layer.js";
import { frontsCanvas } from "./fronts_layer.js";
import { hazardsCanvas, decodeHazards, hazardPopup } from "./hazards_layer.js";
import { decodeGrid, temperatureCanvas, windCanvas, tempScale, windScale, TEMP_RANGE, WIND_RANGE } from "./surface_layer.js";
import { lightningCanvas, lightningScale, mergeMinutes, ageStops, TRAIL } from "./lightning_layer.js";
import { rampColor } from "./ramp.js";
import { linesCanvas, loadLines } from "./lines_layer.js";
import { loopWindow } from "./wx_loop.js";
import { useMapTools, mapboxClass, LayersPanel, LoopBar, SPEEDS, loadLayers, saveLayers } from "./wx_controls.js";

const clock = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];

const FINE_ZOOM = 6;          // radar.py FINE_ZOOM: from here in, the ~2 km shapes for the view
const SURFACE_ZOOMS = [6, 8]; // rtma.py LEVELS: finer grids for the view from these zooms in
const LOADING = 4;            // loop frames fetched at once
const LABELS_Z = 357;         // the place names: over the satellite, warnings and borders, under the radar
const PENDING_MS = 4000;      // a layer the server is still fetching (pending) is asked for again this soon
// What a layer with finer detail from each of `zooms` in is asked for: below the first, the whole US; from there, the
// view padded by half its size and rounded out to whole degrees (so small pans need nothing new), at the zoom tier
// it's in. The radar's shapes (FINE_ZOOM), and the temperature and wind (SURFACE_ZOOMS).
const tierOf = (zoom, zooms) => zooms.filter((z) => zoom >= z).pop() ?? 0;
function viewFor(m, zooms = [FINE_ZOOM]) {
  const zoom = m.getZoom(), tier = tierOf(zoom, zooms);
  if (!tier) return { key: "us", zoom, tier, bbox: null };
  const b = m.getBounds().pad(0.5);
  const bbox = [Math.floor(b.getWest()), Math.floor(b.getSouth()), Math.ceil(b.getEast()), Math.ceil(b.getNorth())]
    .map((v, i) => Math.max(i % 2 ? -90 : -180, Math.min(i % 2 ? 90 : 180, v)));
  return { key: `${tier}:${bbox.join(",")}`, zoom, tier, bbox };
}
const inside = (view, m, zooms = [FINE_ZOOM]) => {   // is the map still within what `view` fetched?
  if (!view || tierOf(m.getZoom(), zooms) !== view.tier) return false;
  if (!view.bbox) return true;
  const b = m.getBounds();
  return b.getWest() >= view.bbox[0] && b.getSouth() >= view.bbox[1] && b.getEast() <= view.bbox[2] && b.getNorth() <= view.bbox[3];
};
// Lightning's box (it's never the whole disk GOES sees): the view padded by half, rounded out to 10° zoomed out and to
// whole degrees from FINE_ZOOM in, so `inside` keeps it while the map moves within it.
function glmViewFor(m) {
  const zoom = m.getZoom(), tier = tierOf(zoom, [FINE_ZOOM]), step = tier ? 1 : 10, b = m.getBounds().pad(0.5);
  const bbox = [Math.floor(b.getWest() / step) * step, Math.floor(b.getSouth() / step) * step, Math.ceil(b.getEast() / step) * step,
    Math.ceil(b.getNorth() / step) * step].map((v, i) => Math.max(i % 2 ? -90 : -180, Math.min(i % 2 ? 90 : 180, v)));
  return { key: `${tier}:${bbox.join(",")}`, zoom, tier, bbox };
}
const radarQuery = (view) => `zoom=${view.zoom}` + (view.bbox ? `&bbox=${view.bbox.join(",")}` : "");
const unpack = (x) => ({ ...x, shapes: decode(x.shapes) });

// A layer of what's in effect now, whatever the timeline shows (fronts, watches and warnings): /api/... fetched every
// `ms` while `on`, again whenever `url` changes (another view), and soon again while the server is still fetching
// it (feed.py's pending).
function useFeed(on, url, ms) {
  const [data, setData] = useState(null);
  useEffect(() => {
    if (!on || !url) return;
    let alive = true, soon = null;
    const load = () => api(url).then((x) => {
      if (!alive) return;
      setData(x);
      clearTimeout(soon);
      if (x.pending) soon = setTimeout(load, PENDING_MS);
    }).catch(() => {});
    const stop = every(load, ms);
    return () => { alive = false; clearTimeout(soon); stop(); };
  }, [on, url]);
  return data;
}

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
  const wrap = useRef(null), box = useRef(null), map = useRef(null);
  const tiles = useRef({ sat: new Map() });     // satellite frame key -> Leaflet tile layer
  const pins = useRef(new Map()), rings = useRef(null), canvas = useRef(null), storms = useRef(null), frontsLayer = useRef(null);
  const hazardsLayer = useRef(null), tempLayer = useRef(null), windLayer = useRef(null), glmLayer = useRef(null), linesLayer = useRef(null);
  const glmShapesLayer = useRef(null);
  const glmShapes = useRef(new Map());             // the smooth lightning: view:minute(:newest, live) -> decoded shapes
  const [ready, setReady] = useState(false);
  const [info, setInfo] = useState(null);          // /api/wx/map: stations and the frames available
  const [live, setLive] = useState(null);          // /api/wx/live: the readings on the pins
  const [strikes, setStrikes] = useState({});      // device -> /api/wx/lightning cells for the last hour
  const [layers, setLayers] = useState(loadLayers);    // the Layers panel's choices, remembered on this device
  const set = (change) => setLayers((l) => ({ ...l, ...change }));
  useEffect(() => saveLayers(layers), [layers]);
  const { radar: showRadar, storms: showStorms, fronts: showFronts, sat, rings: showRings, glm: showGlm, lightningStyle: glmStyle, radarOpacity: opacity } = layers;
  const { hazards: showHazards, hazardsMinor: showMinor, temp: showTemp, tempOpacity, wind: showWind } = layers;
  const [view, setView] = useState(null);          // what the radar shapes are fetched for (viewFor)
  const [surfView, setSurfView] = useState(null);  // ... and the temperature and wind (viewFor SURFACE_ZOOMS)
  const [glmView, setGlmView] = useState(null);    // ... and the lightning (glmViewFor)
  // The lightning held for glmView: minute -> cells, from `from` on, and the last answer's state; bumped as it changes.
  const glmHeld = useRef({ key: null, minutes: new Map(), from: Infinity, q: null, at: null, state: null, pending: false, colors: null });
  const [glmTick, setGlmTick] = useState(0);
  const [live_, setLiveFrame] = useState(null);     // /api/wx/radar for the view: the newest shapes and the storm cells
  const [times, setTimes] = useState([]);          // /api/wx/radar/frames: the loop's kept frames
  const kept = useRef(new Map());                  // the loop's frames loaded for the view: t -> unpacked frame
  const gone = useRef(new Set());                  // listed frames the server can't give (404): left out of the loop
  const [, setLoaded] = useState(0);               // bumped as they arrive
  const [idx, setIdx] = useState(null);            // position in the loop; null = the newest frame
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);           // SPEEDS: ½×, 1×, 2×
  const { fullscreen } = useMapTools(map, ready, wrap);

  useEffect(() => {
    let alive = true;
    const onError = (e) => reportError("error", e.error || e.message);
    const onRejection = (e) => reportError("unhandledrejection", e.reason);
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    leaflet().then((lf) => {
      if (!alive || !box.current) return;
      map.current = baseMap(lf, box.current, { minZoom: 3, labels: LABELS_Z }).setView([39, -96], 4);
      map.current.createPane("wxtemp").style.zIndex = 345;     // the temperature: over the base map, under the rest
      map.current.createPane("wxsat").style.zIndex = 350;      // above the base map, under the radar and markers
      map.current.createPane("wxhazards").style.zIndex = 355;  // over the satellite, under the radar: echoes show through
      map.current.createPane("wxlines").style.zIndex = 356;    // coasts and borders, under the names (LABELS_Z)
      map.current.createPane("wxradar").style.zIndex = 360;
      map.current.createPane("wxwind").style.zIndex = 365;     // over the radar, under the fronts
      map.current.createPane("wxfronts").style.zIndex = 370;     // over the radar, under the storm cells and pins
      map.current.createPane("wxglmshapes").style.zIndex = 374;  // the smooth lightning, under its newest flashes
      map.current.createPane("wxlightning").style.zIndex = 375;  // over the fronts, under the storm cells and pins
      rings.current = lf.L.layerGroup().addTo(map.current);
      storms.current = lf.L.layerGroup().addTo(map.current);
      canvas.current = radarCanvas(lf.L, "wxradar", 'Radar: <a href="https://mrms.nssl.noaa.gov/">NOAA MRMS</a>').addTo(map.current);
      frontsLayer.current = frontsCanvas(lf.L, "wxfronts", 'Fronts: <a href="https://www.wpc.ncep.noaa.gov/html/sfctxt.html">NOAA WPC</a>');
      hazardsLayer.current = hazardsCanvas(lf.L, "wxhazards", 'Alerts: <a href="https://www.weather.gov/">NOAA NWS</a>');
      const rtmaLink = '<a href="https://www.nco.ncep.noaa.gov/pmb/products/rtma/">NOAA RTMA</a>';
      tempLayer.current = temperatureCanvas(lf.L, "wxtemp", `Temperature: ${rtmaLink}`);
      windLayer.current = windCanvas(lf.L, "wxwind", `Wind: ${rtmaLink}`);
      const glmLink = 'Lightning: <a href="https://www.goes-r.gov/spacesegment/glm.html">NOAA GOES-East GLM</a> via <a href="https://www.unidata.ucar.edu/software/tds/">Unidata THREDDS</a>';
      glmLayer.current = lightningCanvas(lf.L, "wxlightning", glmLink);
      glmShapesLayer.current = radarCanvas(lf.L, "wxglmshapes", glmLink, "age");
      linesLayer.current = linesCanvas(lf.L, "wxlines", 'Borders: <a href="https://www.naturalearthdata.com/">Natural Earth</a>');
      // A tap on watches or warnings: what they are there (the most important first), when the layer is on.
      map.current.on("click", (e) => {
        const m = map.current, layer = hazardsLayer.current;
        if (!m?.hasLayer(layer)) return;
        const hits = layer.at(e.latlng);
        if (!hits.length) return;
        const text = (id) => api(`/api/wx/hazards/alert?id=${encodeURIComponent(id)}`);
        lf.L.popup({ maxWidth: 300 }).setLatLng(e.latlng).setContent(hazardPopup(hits, e.latlng.lat, e.latlng.lng, text)).openOn(m);
      });
      const moved = () => {
        setView((v) => (inside(v, map.current) ? v : viewFor(map.current)));
        setSurfView((v) => (inside(v, map.current, SURFACE_ZOOMS) ? v : viewFor(map.current, SURFACE_ZOOMS)));
        setGlmView((v) => (inside(v, map.current) ? v : glmViewFor(map.current)));
      };
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
  // Fronts while they're shown: the analysis is every 3 hours, so a look every 15 minutes is plenty. Watches and
  // warnings every 2 minutes (a new tornado warning matters), for the view as the radar is.
  const fronts = useFeed(showFronts, "/api/wx/fronts", 15 * 60000);
  const hazardsNow = useFeed(showHazards, view && `/api/wx/hazards?${radarQuery(view)}`, 120000);
  const hazards = useMemo(() => decodeHazards(hazardsNow), [hazardsNow]);
  // The temperature and wind: a new analysis every 15 minutes, up ~20 minutes after its time; a look every 5.
  const surfaceQuery = surfView && `zoom=${surfView.tier}` + (surfView.bbox ? `&bbox=${surfView.bbox.join(",")}` : "");
  const tempNow = useFeed(showTemp, surfaceQuery && `/api/wx/temperature?${surfaceQuery}`, 5 * 60000);
  const windNow = useFeed(showWind, surfaceQuery && `/api/wx/wind?${surfaceQuery}`, 5 * 60000);
  const tempGrid = useMemo(() => decodeGrid(tempNow), [tempNow]), windGrid = useMemo(() => decodeGrid(windNow), [windNow]);
  useEffect(() => {
    const m = map.current, layer = frontsLayer.current;
    if (!ready || !m || !layer) return;
    if (!showFronts) { layer.remove(); return; }
    if (!m.hasLayer(layer)) layer.addTo(m);
    layer.set(fronts, css("--bg") || "#fff");
  }, [ready, showFronts, fronts]);
  useEffect(() => {
    const m = map.current, layer = hazardsLayer.current;
    if (!ready || !m || !layer) return;
    if (!showHazards) { layer.remove(); return; }
    if (!m.hasLayer(layer)) layer.addTo(m);
    layer.set(hazards, showMinor);
  }, [ready, showHazards, showMinor, hazards]);
  useEffect(() => {
    const m = map.current, layer = tempLayer.current;
    if (!ready || !m || !layer) return;
    if (!showTemp) { layer.remove(); return; }
    if (!m.hasLayer(layer)) layer.addTo(m);
    layer.setOpacity(tempOpacity).set(tempGrid);
  }, [ready, showTemp, tempOpacity, tempGrid]);
  useEffect(() => {
    const m = map.current, layer = windLayer.current;
    if (!ready || !m || !layer) return;
    if (!showWind) { layer.remove(); return; }
    if (!m.hasLayer(layer)) layer.addTo(m);
    layer.set(windGrid, css("--bg") || "#fff");
  }, [ready, showWind, windGrid]);
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

  // Lightning for the view, every minute while it's shown: from the TRAIL before the timeline's start (or now), then
  // only the last two minutes on (their files may have come in since), all of it again while the server is still
  // catching up (it fills the newest first). Another view starts over.
  const glmFrom = Math.floor(((timeline.length ? timeline[0] : Date.now() / 1000) - TRAIL) / 600) * 600;
  useEffect(() => {
    if (!showGlm || !glmView) return;
    let alive = true, soon = null;
    const held = glmHeld.current;
    if (held.key !== glmView.key) Object.assign(held, { key: glmView.key, minutes: new Map(), from: Infinity, state: null, pending: false });
    const load = () => {
      const key = glmView.key, newest = Math.max(0, ...held.minutes.keys());
      const full = !held.minutes.size || glmFrom < held.from || held.pending;
      const since = full ? glmFrom : Math.max(glmFrom, newest - 120);
      api(`/api/wx/glm?zoom=${glmView.zoom}&bbox=${glmView.bbox.join(",")}&since=${since}`).then((x) => {
        if (!alive || held.key !== key) return;
        const oldest = Math.min(glmFrom, Date.now() / 1000 - TRAIL);
        Object.assign(held, { minutes: mergeMinutes(held.minutes, x, oldest), from: full ? glmFrom : held.from, q: x.q, colors: x.colors,
          at: Date.now() / 1000, state: x, pending: x.pending });
        setGlmTick((n) => n + 1);
        clearTimeout(soon);
        if (x.pending) soon = setTimeout(load, PENDING_MS);
      }).catch(() => {});
    };
    const stop = every(load, 60000);
    return () => { alive = false; clearTimeout(soon); stop(); };
  }, [showGlm, glmView?.key, glmFrom]);
  // Its time: the timeline's while it's played or scrubbed, else now (as of the last answer).
  const glmAt = (playing || idx != null) && t != null ? t : glmHeld.current.at;
  useEffect(() => {
    const m = map.current, layer = glmLayer.current, held = glmHeld.current;
    if (!ready || !m || !layer) return;
    if (!showGlm) { layer.remove(); return; }
    if (!m.hasLayer(layer)) layer.addTo(m);
    layer.set(held.minutes, held.q, glmAt, held.colors, glmStyle);
  }, [ready, showGlm, glmTick, glmAt, glmStyle]);
  // Smooth: the server's shapes for the minute shown and the view, drawn as the radar's are; the last frame stays up
  // while the next loads. Each minute is fetched once, except the live one, again
  // whenever newer flashes have come in; played or scrubbed, the timeline's minutes are fetched ahead, LOADING / 2 at
  // a time, as the radar's frames are.
  const smooth = showGlm && glmStyle === "smooth" && glmView;
  const glmMinute = glmAt == null ? null : Math.floor(glmAt / 60) * 60;
  const glmLive = !(playing || idx != null);
  const shapesKey = (minute, live) => `${glmView.key}:${minute}${live ? `:${glmHeld.current.state?.newest}` : ""}`;
  const fetchShapes = (minute, live) => {
    const kept = glmShapes.current, key = shapesKey(minute, live);
    if (kept.has(key)) return Promise.resolve(kept.get(key));
    return api(`/api/wx/glm/shapes?zoom=${glmView.zoom}&bbox=${glmView.bbox.join(",")}&t=${minute + 59}`).then((x) => {
      const d = decode(x.shapes);
      kept.set(key, d);
      for (const k of [...kept.keys()].slice(0, -90)) kept.delete(k);
      return d;
    });
  };
  useEffect(() => {
    const m = map.current, layer = glmShapesLayer.current;
    if (!ready || !m || !layer) return;
    if (!smooth || glmMinute == null) { layer.remove(); return; }
    if (!m.hasLayer(layer)) layer.addTo(m);
    let alive = true;
    fetchShapes(glmMinute, glmLive).then((d) => {
      if (!alive) return;
      layer.set(d, (age) => rampColor(ageStops(glmHeld.current.colors), age)).setOpacity(0.85);
    }).catch(() => {});
    return () => { alive = false; };
  }, [ready, smooth, glmView?.key, glmMinute, glmLive, glmTick]);
  useEffect(() => {
    if (!smooth || glmLive || !timeline.length) return;
    let alive = true;
    const todo = [...new Set(timeline.map((x) => Math.floor(x / 60) * 60))].reverse();
    const next = () => { const minute = todo.shift(); if (alive && minute != null) fetchShapes(minute, false).catch(() => {}).finally(next); };
    for (let i = 0; i < LOADING / 2; i++) next();
    return () => { alive = false; };
  }, [smooth, glmView?.key, glmLive, timeline[0], timeline.length]);
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

  // The basemap (Auto follows the theme).
  useEffect(() => { if (ready) map.current?.setBasemap(layers.basemap); }, [ready, layers.basemap]);
  // Over the satellite: the names in light text on a dark halo, and the coasts and borders the imagery hides (#95).
  useEffect(() => {
    const m = map.current, layer = linesLayer.current;
    if (!ready || !m || !layer) return;
    m.setLabels(sat ? "dark" : null);
    if (!sat) { layer.remove(); return; }
    let alive = true;
    loadLines().then((lines) => {
      if (!alive) return;
      if (!m.hasLayer(layer)) layer.addTo(m);
      layer.set(lines);
    }).catch(() => {});                            // offline: the satellite without them
    return () => { alive = false; };
  }, [ready, sat]);

  // The smooth radar: the shown frame's shapes, each level colored at its middle (radar_layer.js draws them).
  useEffect(() => {
    if (!ready || !canvas.current) return;
    const shapes = showRadar && colors ? vector?.shapes : null;
    canvas.current.set(shapes, (dbz) => levelColor(colors, dbz)).setOpacity(opacity);
  }, [ready, vector, colors, showRadar, opacity]);

  // Storm cells: a dot colored by strength, and a dashed line with arrows to where it'll be in 30 and 60 minutes.
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
      // The 30 and 60 minute spots: small arrows pointing the way it's heading, under the station pins.
      for (const p of c.kmh ? c.path.slice(1) : []) {
        const head = el("i", "wxstorm-arrow");
        head.style.borderBottomColor = color;
        head.style.transform = `rotate(${c.toward}deg)`;
        L.marker(p, { icon: L.divIcon({ html: head, className: "wxstorm-wrap", iconSize: [8, 8], iconAnchor: [4, 4] }),
          pane: "overlayPane", opacity, interactive: false, keyboard: false }).addTo(group);
      }
      // Where it is now: a small borderless dot, bigger when it's hailing and bigger still when it's rotating.
      L.circleMarker(c.path[0], { radius: c.tvs ? 6 : c.posh >= 30 ? 5 : 3, stroke: false, fillColor: color, fillOpacity: opacity })
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

  // Station strikes in the last hour: a ring per station and distance, fading with age.
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
  const surfaceAt = (showTemp || showWind) && (tempGrid || windGrid)?.valid;
  const glmState = showGlm ? glmHeld.current.state : null;
  const detail = [radarOn && vector?.t && `radar ${clock(vector.t)}`, shownSat && `satellite ${clock(shownSat.t)}`, frontsAt,
    surfaceAt && `surface ${clock(surfaceAt)}`, glmState?.newest && `lightning ${clock(Math.min(glmAt ?? Infinity, glmState.newest + 20))}`]
    .filter(Boolean).join(" · ") || "no imagery";
  const gap = sat && loopWin.end ? Date.now() / 1000 - loopWin.end : 0;
  const shownUnits = live?.units || info?.units;
  const ends = (kind, [lo, hi]) => [lo, hi].map((v) => `${Math.round(unitsFor(shownUnits)[kind][1](v))} ${unitsFor(shownUnits)[kind][0]}`);
  const legend = html`${showRadar && colors && html`<p class="muted heatkey"><span>radar: light</span>
    <i style=${`background: ${radarScale(colors)}; opacity: ${opacity}`}></i><span>heavy / hail</span></p>`}
    ${showTemp && html`<p class="muted heatkey"><span>${ends("temp", TEMP_RANGE)[0]}</span>
      <i style=${`background: ${tempScale()}; opacity: ${tempOpacity}`}></i><span>${ends("temp", TEMP_RANGE)[1]}</span></p>`}
    ${showWind && html`<p class="muted heatkey"><span>wind: calm</span>
      <i style=${`background: ${windScale()}`}></i><span>${ends("speed", WIND_RANGE)[1]}+</span></p>`}
    ${showGlm && html`<p class="muted heatkey"><span>lightning: new</span>
      <i style=${`background: ${lightningScale(glmHeld.current.colors)}`}></i><span>${TRAIL / 60} min</span></p>`}`;
  const notes = html`${showGlm ? (glmState?.error && !glmHeld.current.minutes.size ? " Lightning unavailable right now."
      : glmState?.pending ? " Lightning is loading…"
      : glmState ? ` Lightning: flashes GOES-East's lightning mapper saw in the ${TRAIL / 60} minutes up to the time shown, ${glmStyle === "dots" ? "bigger where there were more" : glmStyle === "smooth" ? "dots for the last 2 minutes" : "brighter where they crowd, crosses for the last 2 minutes"}; about a minute behind.` : "") : ""}
    ${showRings ? " Station strikes: rings at the distance a station measured a strike in the last hour (it can't tell direction)." : ""}
    ${radarOn && live_ && !live_.t ? " The radar starts within a few minutes of the server starting." : ""}
    ${showRadar && live_?.t ? " Radar: NOAA MRMS, cleaned." : ""}${showStorms && live_?.t ? " Dots: storms the NWS radars are tracking." : ""}
    ${radarOn && live_?.t && !times.length ? " The radar's past fills in as frames arrive (one every 2 minutes)." : ""}${satInfo?.error ? " Satellite unavailable right now." : ""}
    ${sat ? " Satellite images run about 30–60 minutes behind." : ""}
    ${showFronts ? (fronts?.valid ? ` Fronts, highs and lows: WPC's surface analysis at ${clock(fronts.valid)} (every 3 hours), whatever time the radar shows.`
      : fronts?.error ? " Fronts unavailable right now." : "") : ""}
    ${showHazards ? (hazardsNow?.error && !hazards?.alerts.length ? " Watches and warnings unavailable right now."
      : hazardsNow?.checked_at ? ` Watches and warnings: the NWS's in effect now${showMinor ? "" : " (advisories and marine alerts hidden)"}, whatever time the radar shows; tap one for details.` : "") : ""}
    ${showTemp || showWind ? (surfaceAt ? ` ${showTemp ? (showWind ? "Temperature and wind" : "Temperature") : "Wind"}: NOAA's surface analysis at ${clock(surfaceAt)} (every 15 minutes), whatever time the radar shows.${showWind ? " Arrows point the way the wind blows." : ""}`
      : (tempNow || windNow)?.error ? " Temperature and wind unavailable right now." : (tempNow || windNow)?.pending ? " Temperature and wind are loading…" : "") : ""}`;
  return html`
    <div ref=${wrap} class=${mapboxClass(fullscreen, "wxmap")}>
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
