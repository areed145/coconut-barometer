// APRS → Trips (#120): a tracked callsign's positions split into trips at its stops (aprs/trips.py), a list of
// them, and one trip's track on a map with its speed and altitude, and a replay: the marker moves along the track
// as the slider (or ▶) goes through the trip's time. Positions between beacons are drawn as straight lines.
import uPlot from "../vendor/uPlot.esm.js";
import { html, useState, useEffect, useRef, api, ago, toast, every, shade, num, useThemeTick } from "./lib.js";
import { leaflet, baseMap } from "./leaflet.js";
import { useMapTools, mapboxClass } from "./wx_controls.js";
import { grid } from "./aprs.js";
import { Table } from "./viz.js";

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const WINDOWS = [["24h", 24], ["7d", 24 * 7], ["30d", 24 * 30], ["90d", 24 * 90], ["1y", 24 * 365]];
const SPEEDS = [30, 120, 600];          // replay: seconds of the trip per second
const U = { metric: { speed: ["km/h", 3.6], height: ["m", 1], dist: ["km", 1] },
            imperial: { speed: ["mph", 2.23694], height: ["ft", 3.28084], dist: ["mi", 0.621371] } };
const unit = (units, kind) => (U[units] || U.metric)[kind];
const show = (units, kind, v, digits = 0) => (v == null ? "–" : `${(v * unit(units, kind)[1]).toFixed(digits)} ${unit(units, kind)[0]}`);
const when = (ts) => new Date(ts * 1000).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const span = (s) => (s == null ? "–" : s < 5400 ? `${Math.round(s / 60)} min` : s < 2 * 86400
  ? `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min` : `${Math.floor(s / 86400)} d ${Math.round((s % 86400) / 3600)} h`);
const where = (place, at) => place || grid(at[0], at[1]);

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// Where the track was at time t: between the two positions around it, in a straight line.
function position(points, t) {
  if (!points.length) return null;
  if (t <= points[0].ts) return points[0];
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1], b = points[i];
    if (t <= b.ts) {
      const f = b.ts > a.ts ? (t - a.ts) / (b.ts - a.ts) : 1;
      return { ts: t, lat: a.lat + (b.lat - a.lat) * f, lon: a.lon + (b.lon - a.lon) * f,
               speed: f < 0.5 ? a.speed : b.speed, altitude: a.altitude != null && b.altitude != null ? a.altitude + (b.altitude - a.altitude) * f : b.altitude };
    }
  }
  return points[points.length - 1];
}

function TripMap({ points, trip, t, units }) {
  const wrap = useRef(null), box = useRef(null), map = useRef(null), layer = useRef(null), marker = useRef(null);
  const [ready, setReady] = useState(false);
  const { fullscreen } = useMapTools(map, ready, wrap);
  const theme = useThemeTick();
  useEffect(() => {
    let alive = true;
    leaflet().then((lf) => {
      if (!alive || !box.current) return;
      map.current = baseMap(lf, box.current, { minZoom: 3 }).setView([39, -96], 4);
      layer.current = lf.L.layerGroup().addTo(map.current);
      setReady(true);
    }).catch(() => toast("Couldn't load the map."));
    return () => { alive = false; map.current?.remove(); map.current = null; };
  }, []);
  useEffect(() => {
    const L = window.L, group = layer.current;
    if (!ready || !group || points.length < 2) return;
    group.clearLayers();
    const top = Math.max(...points.map((p) => p.speed || 0), 1);
    for (let i = 1; i < points.length; i++) {          // each step colored by its speed
      const a = points[i - 1], b = points[i], v = ((a.speed || 0) + (b.speed || 0)) / 2;
      L.polyline([[a.lat, a.lon], [b.lat, b.lon]], { color: shade(v / top), weight: 4, opacity: 0.9 }).addTo(group);
    }
    for (const p of points) L.circleMarker([p.lat, p.lon], { radius: 2.5, color: css("--fg"), weight: 1, fillOpacity: 0.5, interactive: false }).addTo(group);
    const end = (p, label) => L.circleMarker([p.lat, p.lon], { radius: 7, color: css("--fg"), weight: 2, fillColor: css("--bg"), fillOpacity: 1 })
      .bindTooltip(label, { direction: "top" }).addTo(group);
    end(points[0], `Left ${where(trip.from_place, trip.from)} · ${clock(trip.start_ts)}`);
    end(points[points.length - 1], `Arrived ${where(trip.to_place, trip.to)} · ${clock(trip.end_ts)}`);
    const face = el("div", "tripdot");
    marker.current = L.marker([points[0].lat, points[0].lon], { icon: L.divIcon({ html: face, className: "wxpin-wrap", iconSize: [16, 16], iconAnchor: [8, 8] }),
      zIndexOffset: 1000, interactive: false }).addTo(group);
    map.current.fitBounds(points.map((p) => [p.lat, p.lon]), { padding: [40, 40], maxZoom: 15 });
  }, [ready, points, theme]);
  useEffect(() => {
    const p = position(points, t);
    if (p && marker.current) marker.current.setLatLng([p.lat, p.lon]);
  }, [t, points, ready]);
  return html`<div ref=${wrap} class=${mapboxClass(fullscreen)}><div class="mapwrap tripmap" ref=${box}></div></div>`;
}

// Speed (left) and altitude (right) over the trip, with a line at the replay's time.
function TripChart({ points, units, t }) {
  const box = useRef(null), plot = useRef(null), now = useRef(t);
  const theme = useThemeTick();
  now.current = t;
  useEffect(() => {
    const e = box.current;
    if (!e || points.length < 2) return;
    const axis = { stroke: css("--muted"), grid: { stroke: css("--line"), width: 1 }, ticks: { stroke: css("--line") } };
    const [sl, sk] = unit(units, "speed"), [hl, hk] = unit(units, "height");
    const alts = points.some((p) => p.altitude != null);
    const rule = (u) => {
      const x = u.valToPos(now.current, "x", true), { ctx, bbox } = u;
      ctx.save(); ctx.strokeStyle = css("--fg"); ctx.lineWidth = 1.5 * devicePixelRatio;
      ctx.beginPath(); ctx.moveTo(x, bbox.top); ctx.lineTo(x, bbox.top + bbox.height); ctx.stroke(); ctx.restore();
    };
    plot.current = new uPlot({
      width: e.clientWidth, height: 200, legend: { live: true }, cursor: { points: { size: 6 } }, hooks: { draw: [rule] },
      scales: { x: { time: true }, s: { range: (u, mn, mx) => [0, Math.max(1, mx) * 1.1] }, h: { auto: true } },
      axes: [axis, { ...axis, scale: "s", size: 48, label: sl, labelSize: 14 }, ...(alts ? [{ ...axis, scale: "h", side: 1, size: 52, grid: { show: false }, label: hl, labelSize: 14 }] : [])],
      series: [{}, { label: "Speed", scale: "s", stroke: css("--chart-1"), fill: css("--chart-1") + "22", width: 2, points: { show: false },
        value: (u, v) => (v == null ? "–" : `${v.toFixed(0)} ${sl}`) },
        ...(alts ? [{ label: "Altitude", scale: "h", stroke: css("--chart-2"), width: 2, points: { show: false }, value: (u, v) => (v == null ? "–" : `${v.toFixed(0)} ${hl}`) }] : [])],
    }, [points.map((p) => p.ts), points.map((p) => (p.speed == null ? null : p.speed * sk)), ...(alts ? [points.map((p) => (p.altitude == null ? null : p.altitude * hk))] : [])], e);
    const ro = new ResizeObserver(() => plot.current?.setSize({ width: e.clientWidth, height: 200 }));
    ro.observe(e);
    return () => { ro.disconnect(); plot.current?.destroy(); plot.current = null; };
  }, [points, units, theme]);
  useEffect(() => { plot.current?.redraw(false, false); }, [t]);
  return html`<div class="chart" ref=${box}></div>`;
}

function TripView({ call, trip, units }) {
  const [points, setPoints] = useState(null);
  const [t, setT] = useState(trip.start_ts);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(SPEEDS[1]);
  useEffect(() => {
    setPoints(null); setT(trip.start_ts); setPlaying(false);
    api("/api/aprs/track", { params: { callsign: call, start: trip.start_ts, end: trip.end_ts } })
      .then((x) => setPoints(x.points)).catch((e) => toast(e.message));
  }, [call, trip.start_ts, trip.end_ts]);
  useEffect(() => {
    if (!playing) return;
    let last = performance.now(), id;
    const tick = (now) => {
      const dt = (now - last) / 1000;
      last = now;
      setT((x) => {
        const next = Math.min(trip.end_ts, x + dt * rate);
        if (next >= trip.end_ts) setPlaying(false);
        return next;
      });
      id = requestAnimationFrame(tick);
    };
    id = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(id);
  }, [playing, rate, trip.end_ts]);
  if (!points) return html`<div class="card"><p class="muted">Loading the trip…</p></div>`;
  const here = position(points, t);
  return html`<div class="card">
    <h2>${where(trip.from_place, trip.from)} → ${where(trip.to_place, trip.to)}</h2>
    <p class="muted small">${when(trip.start_ts)} to ${clock(trip.end_ts)} · ${show(units, "dist", trip.km, 1)} · moving ${span(trip.moving_s)}
      · average ${show(units, "speed", trip.avg_ms)} · top ${show(units, "speed", trip.top_ms)}${trip.climb_m != null ? ` · climbed ${show(units, "height", trip.climb_m)}` : ""}
      · ${num(trip.packets)} positions, about every ${span(trip.every_s)}</p>
    <${TripMap} points=${points} trip=${trip} t=${t} units=${units} />
    <div class="replay">
      <button class="primary" onClick=${() => { if (t >= trip.end_ts) setT(trip.start_ts); setPlaying(!playing); }}
        aria-label=${playing ? "Pause" : "Play"}>${playing ? "❚❚" : "▶"}</button>
      <input type="range" min=${trip.start_ts} max=${trip.end_ts} step="1" value=${Math.round(t)} aria-label="Replay"
        onInput=${(e) => { setPlaying(false); setT(Number(e.target.value)); }} />
      <span class="mono small">${clock(t)}${here?.speed != null ? ` · ${show(units, "speed", here.speed)}` : ""}</span>
      <select aria-label="Replay speed" value=${rate} onChange=${(e) => setRate(Number(e.target.value))}>
        ${SPEEDS.map((s) => html`<option key=${s} value=${s}>${s}×</option>`)}</select>
    </div>
    <${TripChart} points=${points} units=${units} t=${t} />
  </div>`;
}

export function AprsTrips({ call: wanted }) {
  const [calls, setCalls] = useState(null);
  const [hours, setHours] = useState(24 * 7);
  const [d, setD] = useState(null);
  const [pick, setPick] = useState(null);
  useEffect(() => { api("/api/aprs/stations").then((x) => setCalls(x.callsigns.map((c) => c.callsign))).catch((e) => toast(e.message)); }, []);
  const call = calls && (calls.includes((wanted || "").toUpperCase()) ? wanted.toUpperCase() : calls[0]);
  useEffect(() => {
    if (!call) return;
    let alive = true;
    const load = () => api("/api/aprs/trips", { params: { callsign: call, hours } }).then((x) => alive && setD(x)).catch((e) => alive && toast(e.message));
    const stop = every(load, 60000);
    return () => { alive = false; stop(); };
  }, [call, hours]);
  useEffect(() => setPick(null), [call, hours]);
  if (!calls) return html`<div class="boot">Loading…</div>`;
  if (!calls.length) return html`<div class="card"><h2>No callsigns yet</h2><p class="muted">Trips come from the callsigns in Settings → APRS.</p></div>`;
  const units = d?.units || "metric", trips = d?.callsign === call ? d.trips : [];
  const trip = trips.find((x) => x.start_ts === pick) || null;
  const cols = [
    { key: "start_ts", label: "Left", num: true, fmt: (ts) => when(ts) },
    { key: "from_place", label: "From → to", fmt: (_, r) => `${where(r.from_place, r.from)} → ${where(r.to_place, r.to)}` },
    { key: "km", label: "Distance", num: true, fmt: (v) => show(units, "dist", v, 1) },
    { key: "moving_s", label: "Moving", num: true, fmt: (v) => span(v) },
    { key: "avg_ms", label: "Average", num: true, fmt: (v) => show(units, "speed", v) },
    { key: "top_ms", label: "Top", num: true, fmt: (v) => show(units, "speed", v) },
    { key: "packets", label: "Positions", num: true },
  ];
  const total = trips.reduce((s, x) => s + x.km, 0);
  return html`
    <div class="toolbar">
      <label class="muted small">Callsign <select aria-label="Callsign" value=${call} onChange=${(e) => { location.hash = `#/aprs/trips/${encodeURIComponent(e.target.value)}`; }}>
        ${calls.map((c) => html`<option key=${c} value=${c}>${c}</option>`)}</select></label>
      <span class="seg" aria-label="Window">${WINDOWS.map(([label, h]) => html`<button key=${h} class=${h === hours ? "primary" : ""}
        onClick=${() => setHours(h)}>${label}</button>`)}</span>
    </div>
    ${trip && html`<${TripView} call=${call} trip=${trip} units=${units} />`}
    <div class="card">
      <h2>Trips</h2>
      ${!d || d.callsign !== call ? html`<p class="muted">Loading…</p>` : html`
        <p class="muted small">${num(trips.length)} trip${trips.length === 1 ? "" : "s"}, ${show(units, "dist", total, 0)} in all, from ${num(d.positions)} positions.${" "}
          ${d.first_ts ? `Kept since ${when(d.first_ts)}: APRS-IS has no history.` : ""} Beacons are sparse, so distances and speeds are estimates
          (straight lines between positions).</p>
        ${trips.length ? html`<${Table} rows=${trips} cols=${cols} initial="start_ts" onPick=${(r) => { setPick(r.start_ts); scrollTo({ top: 0, behavior: "smooth" }); }} />`
          : html`<p class="muted">No trips in this window.</p>`}`}
    </div>
    ${d?.callsign === call && (d.stops.length > 0 || d.gaps.length > 0) && html`<div class="card">
      <h2>Stops and gaps</h2>
      <div class="tablewrap"><table class="data"><thead><tr><th>Where</th><th>From</th><th>To</th><th class="num">For</th></tr></thead><tbody>
        ${[...d.stops.map((s) => ({ ...s, kind: "stop" })), ...d.gaps.map((g) => ({ ...g, kind: "gap" }))].sort((a, b) => b.from_ts - a.from_ts)
          .map((s) => html`<tr key=${s.kind + s.from_ts}>
            <td>${s.kind === "gap" ? html`<span class="muted">No packets (moved ${show(units, "dist", s.km, 1)})</span>` : where(s.place, [s.lat, s.lon])}</td>
            <td>${when(s.from_ts)}</td><td>${when(s.to_ts)}</td><td class="num">${span(s.to_ts - s.from_ts)}</td></tr>`)}
      </tbody></table></div>
    </div>`}`;
}
