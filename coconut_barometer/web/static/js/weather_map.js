// The Weather → Map tab: radar and GOES-East satellite over the stations, with the stations' current readings
// and their recent lightning. Radar and NWS storm tracks are independent layers; radar and satellite frames
// share one timeline. A Tempest reports how far away a strike was but not which way, so lightning is drawn as
// a ring at that distance around the station.
// Markers and popups are built as elements (no HTML strings with style attributes) to suit the page's CSP.
import { html, useState, useEffect, useRef, api, toast, every } from "./lib.js";
import { leaflet, baseMap } from "./leaflet.js";

const clock = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const SATS = [["", "No satellite"], ["geocolor", "Satellite: color"], ["infrared", "Satellite: infrared"]];
const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const hex = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
// The color at `dbz`, blended between the nearest stops of Settings → Display → Radar colors.
function radarColor(stops, dbz) {
  const s = [...stops].sort((a, b) => a.dbz - b.dbz);
  if (dbz <= s[0].dbz) return s[0].color;
  const j = s.findIndex((x) => x.dbz >= dbz);
  if (j < 0) return s[s.length - 1].color;
  const [a, b] = [s[j - 1], s[j]], k = (dbz - a.dbz) / (b.dbz - a.dbz);
  return `rgb(${hex(a.color).map((x, i) => Math.round(x + (hex(b.color)[i] - x) * k)).join(",")})`;
}
const radarScale = (stops) => {
  const s = [...stops].sort((a, b) => a.dbz - b.dbz), lo = s[0].dbz, hi = s[s.length - 1].dbz;
  return `linear-gradient(90deg, ${s.map((x) => `${x.color} ${Math.round(((x.dbz - lo) / (hi - lo || 1)) * 100)}%`).join(", ")})`;
};
// The NWS reflectivity colors, light rain (5 dBZ) to hail (70+).

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
  const tiles = useRef({ sat: new Map() });                       // frame key -> Leaflet tile layer
  const pins = useRef(new Map()), rings = useRef(null), shapes = useRef(null), storms = useRef(null);
  const [ready, setReady] = useState(false);
  const [info, setInfo] = useState(null);          // /api/wx/map: stations and the frames available
  const [live, setLive] = useState(null);          // /api/wx/live: the readings on the pins
  const [strikes, setStrikes] = useState({});      // device -> /api/wx/lightning cells for the last hour
  const [showRadar, setShowRadar] = useState(true);
  const [showStorms, setShowStorms] = useState(true);
  const [live_, setLiveFrame] = useState(null);     // /api/wx/radar: the configured colors and newest data
  const [loop, setLoop] = useState({ times: [], data: {} });   // /api/wx/radar/frames: the kept frames, and those loaded
  const [sat, setSat] = useState("");
  const [showRings, setShowRings] = useState(true);
  const [opacity, setOpacity] = useState(0.7);
  const [idx, setIdx] = useState(null);            // position in the loop; null = the newest frame
  const [playing, setPlaying] = useState(false);

  useEffect(() => {
    let alive = true;
    const report = (source, error) => {
      try {
        const e = error instanceof Error ? error : new Error(String(error || "Unknown map error"));
        fetch("/api/wx/map/errors", { method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source, message: e.message.slice(0, 500), stack: (e.stack || "").slice(0, 6000) }) }).catch(() => {});
      } catch { /* reporting must not cause another map error */ }
    };
    const onError = (e) => report("error", e.error || e.message);
    const onRejection = (e) => report("unhandledrejection", e.reason);
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    leaflet().then((lf) => {
      if (!alive || !box.current) return;
      map.current = baseMap(lf, box.current, { minZoom: 3 }).setView([39, -96], 4);
      map.current.createPane("wxsat").style.zIndex = 350;      // above the base map, under the radar and markers
      map.current.createPane("wxradar").style.zIndex = 360;
      rings.current = lf.L.layerGroup().addTo(map.current);
      storms.current = lf.L.layerGroup().addTo(map.current);
      setReady(true);
    }).catch((e) => { if (alive) { toast("Couldn't load the map."); report("error", e); } });
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
    const smooth = () => api("/api/wx/radar").then((x) => alive && setLiveFrame(x)).catch(() => {});
    const kept = () => api("/api/wx/radar/frames").then((x) => alive && setLoop((l) => ({ ...l, times: x.frames.map((f) => f.t) }))).catch(() => {});
    const stopFrames = every(frames, 300000), stopReadings = every(readings, 30000), stopSmooth = every(smooth, 120000), stopKept = every(kept, 120000);
    return () => { alive = false; stopFrames(); stopReadings(); stopSmooth(); stopKept(); };
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

  // Every selected imagery layer uses the same timestamps; layers with fewer frames hold their latest frame.
  const timeline = [...new Set([...(showRadar || showStorms ? loop.times : []), ...satFrames.map((f) => f.t)])].sort((a, b) => a - b);
  const timelineKey = timeline.join(",");
  const at = timeline.length ? Math.min(idx ?? timeline.length - 1, timeline.length - 1) : -1;
  const t = at >= 0 ? timeline[at] : null;
  const radarTimes = showRadar || showStorms ? loop.times.map((t) => ({ t })) : [];
  const shownRadar = t != null && radarTimes.length ? frameAt(radarTimes, t) : null;
  const vector = shownRadar ? loop.data[shownRadar.t] || null : null;
  const colors = live_?.colors;
  const shownSat = satFrames.length && t != null ? frameAt(satFrames, t) : null;
  useEffect(() => {
    for (const [k, layer] of tiles.current.sat) layer.setOpacity(shownSat && k === `${sat}:${shownSat.time}` ? Math.min(1, opacity + 0.2) : 0);
  });

  // The loop's frames are fetched once each (the server keeps them for the last hour); older ones are dropped.
  const asked = useRef(new Map());
  useEffect(() => {
    if (!showRadar && !showStorms) return;
    let alive = true;
    const requests = new Map();
    for (const t of loop.times) if (!loop.data[t] && !asked.current.has(t)) {
      const request = {};
      requests.set(t, request);
      asked.current.set(t, request);
      api(`/api/wx/radar/frame?t=${t}`).then((x) => {
        if (alive && asked.current.get(t) === request) setLoop((l) => ({ ...l, data: { ...l.data, [t]: x } }));
      }).catch(() => {
        if (asked.current.get(t) === request) asked.current.delete(t);
      });
    }
    setLoop((l) => {
      const keep = Object.keys(l.data).filter((k) => l.times.includes(+k));
      return keep.length === Object.keys(l.data).length ? l : { ...l, data: Object.fromEntries(keep.map((k) => [k, l.data[k]])) };
    });
    return () => {
      alive = false;
      for (const [t, request] of requests) if (!loop.data[t] && asked.current.get(t) === request) asked.current.delete(t);
    };
  }, [showRadar, showStorms, loop.times]);

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
  }, [playing, timelineKey]);

  // Radar: a filled shape per 5 dBZ band, each colored at its middle.
  useEffect(() => {
    const m = map.current, L = window.L;
    if (!ready || !m) return;
    shapes.current?.remove();
    shapes.current = null;
    if (!showRadar || !colors || !vector?.bands?.features?.length) return;
    shapes.current = L.geoJSON(vector.bands, { pane: "wxradar", interactive: false, renderer: L.canvas({ pane: "wxradar" }),
      style: (f) => ({ stroke: false, fill: true, fillColor: radarColor(colors, f.properties.dbz + 2.5), fillOpacity: opacity }),
      attribution: 'Radar: <a href="https://mrms.nssl.noaa.gov/">NOAA MRMS</a>' }).addTo(m);
  }, [ready, vector, colors, showRadar, opacity]);

  // Storm cells: a dot colored by strength, and a dashed line to where it'll be in 30 and 60 minutes.
  useEffect(() => {
    const L = window.L, group = storms.current;
    if (!ready || !group) return;
    group.clearLayers();
    if (!showStorms || !vector || !colors) return;
    const unit = live_.units;
    for (const c of vector.cells) {
      const color = radarColor(colors, c.dbz || 0);
      const moving = c.kmh ? `moving ${COMPASS[Math.round(c.toward / 45) % 8]} at ${show(unit, "speed", c.kmh / 3.6, 0)}` : "barely moving";
      const tip = [`${c.dbz} dBZ storm, ${moving}`, c.posh >= 30 && `hail likely (${c.posh}% severe)`, c.tvs && "tornado vortex signature",
        c.kmh && "dashes: 30 and 60 minutes on its present course", c.valid && `scanned ${clock(c.valid)}`].filter(Boolean).join(" · ");
      if (c.kmh) L.polyline(c.path, { color, weight: 2, dashArray: "4 6", interactive: false }).addTo(group);
      for (const p of c.kmh ? c.path.slice(1) : []) L.circleMarker(p, { radius: 2, color, weight: 1, fillOpacity: 1, interactive: false }).addTo(group);
      L.circleMarker(c.path[0], { radius: c.tvs || c.posh >= 30 ? 8 : 6, color: css("--fg"), weight: c.tvs ? 3 : 1, fillColor: color, fillOpacity: 0.95 })
        .bindTooltip(tip, { sticky: true }).addTo(group);
    }
  }, [ready, vector, colors, showStorms, live]);

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
      <span class="seg" aria-label="Map layers">
        <button class=${showRadar ? "primary" : ""} aria-pressed=${showRadar} onClick=${() => { setShowRadar(!showRadar); setIdx(null); }}>Radar</button>
        <button class=${showStorms ? "primary" : ""} aria-pressed=${showStorms} onClick=${() => { setShowStorms(!showStorms); setIdx(null); }}>Storm tracks</button>
      </span>
      <span class="seg" aria-label="Satellite">${SATS.map(([k, l]) => html`<button key=${k} class=${k === sat ? "primary" : ""} aria-pressed=${k === sat}
        onClick=${() => { setSat(k); setIdx(null); }}>${l}</button>`)}</span>
      <button class=${showRings ? "primary" : ""} aria-pressed=${showRings} onClick=${() => setShowRings(!showRings)}>Lightning${total ? ` · ${total}` : ""}</button>
      <span class="wxloop">
        <button disabled=${timeline.length < 2} onClick=${() => setPlaying(!playing)} aria-label=${playing ? "Pause" : "Play"}>${playing ? "❚❚" : "▶"}</button>
        <input type="range" aria-label="Frame" min="0" max=${Math.max(0, timeline.length - 1)} value=${Math.max(0, at)} disabled=${timeline.length < 2}
          onInput=${(e) => { setPlaying(false); setIdx(Number(e.target.value)); }} />
        <span class="muted">${[shownRadar && `radar ${clock(shownRadar.t)}`,
          shownSat && `satellite ${clock(shownSat.t)}`].filter(Boolean).join(" · ") || "no imagery"}</span>
      </span>
      <label class="muted wxopacity">Opacity <input type="range" min="0.2" max="1" step="0.05" value=${opacity} onInput=${(e) => setOpacity(Number(e.target.value))} /></label>
    </div>
    <div class="mapwrap" ref=${box}></div>
    <p class="muted heatkey">${showRadar && colors && html`<span>radar: light</span><i style=${`background: ${radarScale(colors)}`}></i><span>heavy / hail</span>`}
      <span>${showRings ? "Rings: lightning in the last hour, at the distance the station measured (it can't tell direction)." : ""}
        ${showRadar && vector ? " Radar: NOAA MRMS, cleaned." : ""}${showStorms && vector ? " Dots: storms the NWS radars are tracking nationwide." : ""}
        ${(showRadar || showStorms) && !loop.times.length ? " Radar and storm tracks fill in as frames arrive (one every 2 minutes)." : ""}${satInfo?.error ? " Satellite unavailable right now." : ""}
        ${sat ? " Satellite images run about 30–60 minutes behind." : ""}</span></p>`;
}
