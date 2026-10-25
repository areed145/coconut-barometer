// APRS → Igate: the packets APRS-IS credits the igate in Settings → APRS with gating. A map counts them on a
// lat/lon grid (color on a log scale, as on the photo map), with the igate and its effective range; a chart counts
// them by distance from the igate, with the Weibull fit whose 90% distance is that effective range (igate.py).
// Distance is from direct packets by default: a digipeated one says where the sender was, not how far the igate hears.
// By direction (#115): the same fit per sector of bearing, as an outline on the map and a polar chart.
// Below, who it heard (#118): stations per day, new and regular, and what kind each one is (from its symbol).
import uPlot from "../vendor/uPlot.esm.js";
import { html, useState, useEffect, useRef, api, ago, toast, every, shade, logShare, num, useThemeTick, signedIn } from "./lib.js";
import { leaflet, baseMap } from "./leaflet.js";
import { grid } from "./aprs.js";
import { useMapTools, mapboxClass } from "./wx_controls.js";
import { Bars, Table } from "./viz.js";
import { Chart } from "./chart.js";

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const WINDOWS = [["24h", 24], ["7d", 24 * 7], ["30d", 24 * 30], ["90d", 24 * 90], ["1y", 24 * 365], ["All", 24 * 3650]];
const CELLS = [["0.02", "0.02°"], ["0.05", "0.05°"], ["0.1", "0.1°"], ["0.25", "0.25°"], ["0.5", "0.5°"],
  ["subsquare", "Grid subsquare (EM10dg)"], ["square", "Grid square (EM10)"]];
const MI = 0.621371;
const len = (units, km, digits) => (km == null ? "–" : units === "imperial"
  ? `${(km * MI).toFixed(digits ?? (km * MI < 10 ? 1 : 0))} mi` : `${km.toFixed(digits ?? (km < 10 ? 1 : 0))} km`);
const when = (ts) => new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// A cell's name: its grid locator when the cells are grid squares, else its corner coordinates.
function cellName(lat, lon, cell, cells) {
  const mid = grid(lat + cells.dlat / 2, lon + cells.dlon / 2);
  if (cell === "square") return mid.slice(0, 4);
  if (cell === "subsquare") return mid;
  return `${lat.toFixed(2)}…${(lat + cells.dlat).toFixed(2)}, ${lon.toFixed(2)}…${(lon + cells.dlon).toFixed(2)}`;
}

// The point `km` from (lat, lon) along bearing `deg` (great circle).
function ahead(lat, lon, deg, km) {
  const r = Math.PI / 180, d = km / 6371, b = deg * r, p1 = lat * r, l1 = lon * r;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [p2 / r, l2 / r];
}

// Each fitted sector's arc at radius pick(row), joined to its neighbours' where they're fitted too: [[from°, km], …] runs.
function outline(rows, pick) {
  const n = rows.length, width = 360 / n, runs = [];
  const start = rows.findIndex((r) => !pick(r));       // begin after a gap, so no run wraps past north half-drawn
  for (let k = 0; k < n; k++) {
    const r = rows[(start + 1 + k + n) % n], km = pick(r);
    if (!km) { runs.push([]); continue; }
    if (!runs.length) runs.push([]);
    const from = (r.sector - 0.5) * width, steps = Math.max(2, Math.ceil(width / 5));
    for (let i = 0; i <= steps; i++) runs[runs.length - 1].push([from + (width * i) / steps, km]);
  }
  const closed = start < 0;
  return { runs: runs.filter((x) => x.length), closed };
}

function IgateMap({ d, cell, sectors }) {
  const wrap = useRef(null), box = useRef(null), map = useRef(null), layer = useRef(null), fitted = useRef("");
  const [ready, setReady] = useState(false);
  const { fullscreen } = useMapTools(map, ready, wrap);       // ⌖ and ⛶, as on the other maps (#82)
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
    if (!ready || !group) return;
    group.clearLayers();
    const { dlat, dlon, rows } = d.cells;
    const top = rows.reduce((m, r) => Math.max(m, r[2]), 1);
    const bounds = [];
    // Least first, so the busiest cells draw on top.
    for (const [lat, lon, n, stations] of [...rows].reverse()) {
      const corners = [[lat, lon], [lat + dlat, lon + dlon]];
      const pop = el("div", "wxpop");
      pop.append(el("b", null, cellName(lat, lon, cell, d.cells)),
        el("div", null, `${num(n)} packet${n === 1 ? "" : "s"} · ${num(stations)} station${stations === 1 ? "" : "s"}`));
      if (d.home) {
        const r = Math.PI / 180, mlat = lat + dlat / 2, mlon = lon + dlon / 2;
        const h = Math.sin((mlat - d.home.lat) * r / 2) ** 2 + Math.cos(d.home.lat * r) * Math.cos(mlat * r) * Math.sin((mlon - d.home.lon) * r / 2) ** 2;
        pop.append(el("div", "muted", `${len(d.units, 12742 * Math.asin(Math.sqrt(h)))} from the igate`));
      }
      const color = shade(logShare(n, top, 1));
      L.rectangle(corners, { color, weight: 1, opacity: 0.9, fillColor: color, fillOpacity: 0.55 }).bindPopup(pop).addTo(group);
      bounds.push(...corners);
    }
    if (d.home) {
      const fit = d.distance?.fit;
      if (fit) {
        L.circle([d.home.lat, d.home.lon], { radius: fit.effective_km * 1000, color: css("--fg"), weight: 1.5, dashArray: "6 6", fill: false, interactive: false }).addTo(group);
      }
      const rows = sectors?.rows;
      if (rows?.some((r) => r.fit)) {
        const { runs, closed } = outline(rows, (r) => r.fit?.effective_km);
        for (const run of runs) {
          const pts = run.map(([deg, km]) => ahead(d.home.lat, d.home.lon, deg, km));
          (closed ? L.polygon : L.polyline)(pts, { color: css("--chart-2"), weight: 2, fill: false, interactive: false }).addTo(group);
        }
      }
      const face = el("div", "aprspin"), dot = el("i");
      dot.hidden = true;
      face.append(dot, el("b", null, d.igate));
      const pop = el("div", "wxpop");
      pop.append(el("b", null, d.igate), el("div", null, `${d.home.lat.toFixed(4)}, ${d.home.lon.toFixed(4)} · ${grid(d.home.lat, d.home.lon)}`),
        el("div", "muted", d.home.source === "setting" ? "from Settings → APRS" : `from its beacon, ${ago(d.home.ts)}`),
        ...(fit ? [el("div", null, `Effective range ${len(d.units, fit.effective_km)} (dashed)`)] : []));
      L.marker([d.home.lat, d.home.lon], { icon: L.divIcon({ html: face, className: "wxpin-wrap", iconSize: null, iconAnchor: [14, 14] }), zIndexOffset: 1000 })
        .bindPopup(pop).addTo(group);
      bounds.push([d.home.lat, d.home.lon]);
    }
    // Fit to the data once per igate, and again when the first cells arrive.
    const key = `${d.igate}:${bounds.length > 1}`;
    if (bounds.length && fitted.current !== key) {
      fitted.current = key;
      map.current.fitBounds(bounds, { padding: [40, 40], maxZoom: 11 });
    }
  }, [ready, d, cell, theme, sectors]);
  const top = d.cells.rows.reduce((m, r) => Math.max(m, r[2]), 0);
  return html`<div ref=${wrap} class=${mapboxClass(fullscreen, "igatemapbox")}>
    <div class="mapwrap igatemap" ref=${box}></div>
    ${top > 0 && html`<div class="igatescale" aria-label="Packets per cell">
      <span>1</span><i style=${`background: linear-gradient(to right, ${[0, 0.25, 0.5, 0.75, 1].map((t) => shade(t)).join(", ")})`}></i>
      <span>${num(top)} packets per cell (log scale)</span></div>`}
  </div>`;
}

// Count per distance bin (bars) and the fitted Weibull's expected count per bin (line); a rule marks the effective range.
function RangeChart({ dist, units, count }) {
  const box = useRef(null);
  const theme = useThemeTick();
  useEffect(() => {
    const el = box.current;
    if (!el || !dist.counts?.length) return;
    const k = units === "imperial" ? MI : 1, label = units === "imperial" ? "mi" : "km", w = dist.bin_km;
    const xs = dist.counts.map((_, i) => (i + 0.5) * w * k);
    const fit = dist.fit;
    const axis = { stroke: css("--muted"), grid: { stroke: css("--line"), width: 1 }, ticks: { stroke: css("--line") } };
    const rule = (u) => {
      if (!fit) return;
      const { ctx, bbox } = u, x = u.valToPos(fit.effective_km * k, "x", true);
      if (x < bbox.left || x > bbox.left + bbox.width) return;
      ctx.save();
      ctx.strokeStyle = css("--fg"); ctx.lineWidth = 1.5 * devicePixelRatio; ctx.setLineDash([6 * devicePixelRatio, 6 * devicePixelRatio]);
      ctx.beginPath(); ctx.moveTo(x, bbox.top); ctx.lineTo(x, bbox.top + bbox.height); ctx.stroke();
      ctx.setLineDash([]); ctx.fillStyle = css("--fg"); ctx.font = `${11 * devicePixelRatio}px sans-serif`;
      ctx.textAlign = x > bbox.left + bbox.width * 0.75 ? "right" : "left";
      ctx.fillText(` 90% · ${len(units, fit.effective_km)} `, x, bbox.top + 14 * devicePixelRatio);
      ctx.restore();
    };
    const what = count === "stations" ? "Stations" : "Packets";
    const lo = (i) => `${(i * w * k).toFixed(w * k < 1 ? 1 : 0)}–${((i + 1) * w * k).toFixed(w * k < 1 ? 1 : 0)} ${label}`;
    const plot = new uPlot({
      width: el.clientWidth, height: 260, legend: { live: true }, cursor: { points: { size: 6 } },
      scales: { x: { time: false, range: [0, dist.counts.length * w * k] }, y: { range: (u, mn, mx) => [0, Math.max(1, mx) * 1.08] } },
      hooks: { draw: [rule] },
      axes: [{ ...axis, label: `Distance from the igate (${label})`, labelSize: 18, labelFont: "11px sans-serif" },
        { ...axis, size: 52, values: (u, vals) => vals.map((v) => (v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + "k" : v)) }],
      series: [
        { label: "Distance", value: (u, v, s, i) => (i == null ? "–" : lo(i)) },
        { label: what, stroke: css("--chart-1"), fill: css("--chart-1") + "aa", width: 1, points: { show: false },
          paths: uPlot.paths.bars({ size: [0.9, 64], gap: 2 }), value: (u, v) => (v == null ? "–" : num(v)) },
        ...(fit ? [{ label: "Weibull fit", stroke: css("--chart-2"), width: 2, points: { show: false },
          value: (u, v) => (v == null ? "–" : v.toFixed(1)) }] : []),
      ],
    }, [xs, dist.counts, ...(fit ? [fit.curve] : [])], el);
    const ro = new ResizeObserver(() => plot.setSize({ width: el.clientWidth, height: 260 }));
    ro.observe(el);
    return () => { ro.disconnect(); plot.destroy(); };
  }, [dist, units, count, theme]);
  return html`<div class="chart" ref=${box}></div>`;
}

const TYPES = { mobile: "Mobiles", home: "Home stations", weather: "Weather stations", digi: "Digipeaters", igate: "Igates",
  balloon: "Balloons and rockets", object: "Objects and items only", other: "Other" };
const TYPE = { mobile: "Mobile", home: "Home", weather: "Weather", digi: "Digipeater", igate: "Igate", balloon: "Balloon or rocket",
  object: "Objects only", other: "Other" };
const SHOWN = 50;
const aprsfi = (call) => html`<a href=${`https://aprs.fi/info/a/${encodeURIComponent(call)}`} target="_blank" rel="noopener">${call}</a>`;

// Who the igate heard over the window: its own request, so the map and range draw first.
function Stations({ hours, units }) {
  const [s, setS] = useState(null);
  const [only, setOnly] = useState("all");        // all, new, regular
  const [type, setType] = useState("");
  const [all, setAll] = useState(false);
  useEffect(() => {
    let alive = true;
    const load = () => api("/api/aprs/igate/stations", { params: { hours } }).then((x) => alive && setS(x)).catch(() => {});
    const stop = every(load, 60000);
    return () => { alive = false; stop(); };
  }, [hours]);
  if (!s?.stations) return null;
  const rows = s.stations.filter((x) => (only === "all" || x[only]) && (!type || x.type === type));
  const bars = Object.entries(s.types).filter(([, n]) => n).map(([t, n]) => ({ label: TYPES[t], key: t, n }));
  const cols = [
    { key: "callsign", label: "Station", fmt: (c, r) => html`${aprsfi(c)}${r.new ? html` <span class="badge">new</span>` : ""}${
      r.regular ? html` <span class="badge">regular</span>` : ""}` },
    { key: "type", label: "Type", fmt: (t) => TYPE[t] },
    { key: "packets", label: "Packets", num: true },
    { key: "km", label: "Typically", num: true, fmt: (km) => len(units, km) },
    { key: "first_ever", label: "First heard", num: true, fmt: (t) => when(t) },
    { key: "last_ts", label: "Last heard", num: true, fmt: (t) => ago(t) },
  ];
  const per = s.bucket > 86400 ? "week" : "day";
  return html`<div class="card">
    <h2>Stations heard</h2>
    <p class="muted small">${num(s.count)} station${s.count === 1 ? "" : "s"} · ${num(s.new)} new (never heard before) ·
      ${num(s.regular)} regular${s.regular === 1 ? "" : "s"} (heard on 7 of the last 14 days)</p>
    ${s.days.length > 2 && html`<${Chart} height=${180} days=${s.days.map((d) => d[0])} series=${[
      { label: `Stations per ${per}`, values: s.days.map((d) => d[1]), color: "--chart-1", bars: true },
      { label: "New", values: s.days.map((d) => d[2]), color: "--chart-2", fill: false }]} />`}
    <h3>By type</h3>
    <${Bars} rows=${bars} value="n" sorted onPick=${(r) => setType(type === r.key ? "" : r.key)}
      title=${(r) => `Show only ${r.label.toLowerCase()} (again: all)`} />
    <div class="toolbar">
      <span class="seg" aria-label="Which stations">${[["all", "All"], ["new", "New"], ["regular", "Regulars"]].map(([v, label]) => html`<button
        key=${v} class=${only === v ? "primary" : ""} onClick=${() => setOnly(v)}>${label}</button>`)}</span>
      ${type && html`<button onClick=${() => setType("")}>${TYPES[type]} ✕</button>`}
    </div>
    ${rows.length ? html`<${Table} rows=${all ? rows : rows.slice(0, SHOWN)} cols=${cols} initial="packets" />`
      : html`<p class="muted">No stations like that in this window.</p>`}
    ${!all && rows.length > SHOWN && html`<button onClick=${() => setAll(true)}>Show all ${num(rows.length)}</button>`}
    ${s.count > s.stations.length && html`<p class="muted small">The busiest ${num(s.stations.length)} are listed.</p>`}
  </div>`;
}

const COMPASS16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
const direction = (sector, n) => COMPASS16[(sector * 16) / n];

// One wedge per sector out to its effective range, a dashed line at its 50%, and sectors without enough packets
// greyed out to the edge. North up, clockwise.
function Polar({ s, units }) {
  const size = 320, c = size / 2, R = c - 22, n = s.rows.length, w = 360 / n;
  const k = units === "imperial" ? MI : 1, unit = units === "imperial" ? "mi" : "km";
  const top = Math.max(...s.rows.map((r) => r.fit?.effective_km || 0), 1) * k;
  const step = [1, 2, 5].flatMap((m) => [1, 10, 100, 1000].map((p) => m * p)).sort((a, b) => a - b).find((x) => top / x <= 4);
  const max = Math.ceil(top / step) * step, scale = (km) => (km * k * R) / max;
  const rings = s.rows.some((r) => r.fit) ? Array.from({ length: max / step }, (_, i) => (i + 1) * step) : [];   // no fit: no scale
  const xy = (deg, r) => [c + r * Math.sin((deg * Math.PI) / 180), c - r * Math.cos((deg * Math.PI) / 180)];
  const wedge = (from, r) => {
    const [x1, y1] = xy(from, r), [x2, y2] = xy(from + w, r);
    return `M${c},${c} L${x1.toFixed(1)},${y1.toFixed(1)} A${r.toFixed(1)},${r.toFixed(1)} 0 0 1 ${x2.toFixed(1)},${y2.toFixed(1)} Z`;
  };
  const half = outline(s.rows, (r) => r.fit?.r50_km);
  const line = (run) => run.map(([deg, km], i) => `${i ? "L" : "M"}${xy(deg, scale(km)).map((v) => v.toFixed(1)).join(",")}`).join(" ")
    + (half.closed ? " Z" : "");
  return html`<svg class="igatepolar" viewBox=${`0 0 ${size} ${size}`} role="img" aria-label="Effective range by direction">
    ${rings.map((v) => html`<g key=${v}>
      <circle cx=${c} cy=${c} r=${(v * R) / max} class="ring" />
</g>`)}
    ${s.rows.map((r) => html`<path key=${r.sector} d=${wedge((r.sector - 0.5) * w, r.fit ? scale(r.fit.effective_km) : R)}
      class=${r.fit ? "wedge" : "wedge thin"}><title>${direction(r.sector, n)}: ${r.fit
        ? `effective range ${len(units, r.fit.effective_km)}, ${num(r.n)} packets` : `not enough data (${num(r.n)} packets)`}</title></path>`)}
    ${half.runs.map((run, i) => html`<path key=${i} d=${line(run)} class="half" />`)}
    ${rings.map((v) => html`<text key=${v} x=${c + 3}
      y=${c - (v * R) / max + 11} class="tick">${v} ${unit}</text>`)}
    ${[["N", 0], ["E", 90], ["S", 180], ["W", 270]].map(([t, deg]) => {
      const [x, y] = xy(deg, R + 12);
      return html`<text key=${t} x=${x} y=${y} class="compass">${t}</text>`;
    })}
  </svg>`;
}

function Directions({ s, units, n, setN }) {
  const fitted = s.rows.filter((r) => r.fit).length;
  return html`<div class="card">
    <h2>Range by direction</h2>
    <div class="toolbar"><span class="seg" aria-label="Sectors">${[8, 16].map((v) => html`<button key=${v}
      class=${v === n ? "primary" : ""} onClick=${() => setN(v)}>${v} sectors</button>`)}</span></div>
    ${s.rows.length ? html`<div class="igatedirs">
      <div><${Polar} s=${s} units=${units} />
        <p class="muted small">Filled: each sector's effective range (90% of its fit), also outlined on the map. Dashed: half
          within. Grey: fewer than ${s.min_packets} packets, not enough for a fit.</p></div>
      <div class="tablewrap"><table class="data"><thead><tr><th>Direction</th><th class="num">Packets</th><th class="num">Median</th>
        <th class="num">Effective range</th></tr></thead><tbody>
        ${s.rows.map((r) => html`<tr key=${r.sector}><td>${direction(r.sector, n)}</td><td class="num">${num(r.n)}</td>
          <td class="num">${len(units, r.p50_km)}</td>
          <td class="num">${r.fit ? len(units, r.fit.effective_km) : html`<span class="muted">not enough data</span>`}</td></tr>`)}
      </tbody></table></div></div>` : html`<p class="muted">Directions wait for the igate's position.</p>`}
    ${s.rows.length > 0 && !fitted && html`<p class="muted small">No sector has ${s.min_packets} packets yet in this window: try a longer one.</p>`}
  </div>`;
}

const Tile = ({ label, value, note }) => html`<div class="kpi"><span>${label}</span><b>${value}</b>${note && html`<small> ${note}</small>`}</div>`;

export function Igate() {
  const [hours, setHours] = useState(24 * 30);
  const [direct, setDirect] = useState(true);
  const [count, setCount] = useState("packets");
  const [cell, setCell] = useState("0.1");
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const [n, setN] = useState(8);
  const [sectors, setSectors] = useState(null);
  useEffect(() => {
    let alive = true;
    const load = () => api("/api/aprs/igate/sectors", { params: { hours, direct, sectors: n } })
      .then((x) => alive && setSectors(x)).catch(() => {});
    const stop = every(load, 60000);
    return () => { alive = false; stop(); };
  }, [hours, direct, n]);
  useEffect(() => {
    let alive = true;
    const load = () => api("/api/aprs/igate", { params: { hours, direct, count, cell } })
      .then((x) => { if (alive) { setD(x); setErr(""); } }).catch((e) => alive && setErr(e.message));
    const stop = every(load, 60000);
    return () => { alive = false; stop(); };
  }, [hours, direct, count, cell]);

  if (!d) return err ? html`<p class="problem">${err}</p>` : html`<div class="boot">Loading…</div>`;
  if (!d.igate) return html`<div class="card"><h2>No igate yet</h2>
    ${signedIn() && html`<p>Set the igate to follow under <a href="#/settings/aprs">Settings → APRS</a>.</p>`}</div>`;
  const t = d.totals, dist = d.distance, fit = dist?.fit, win = WINDOWS.find((w) => w[1] === hours)[0];
  const what = count === "stations" ? "stations (each at its farthest)" : "packets";
  return html`
    <div class="toolbar">
      <span class="seg" aria-label="Window">${WINDOWS.map(([label, h]) => html`<button key=${h}
        class=${h === hours ? "primary" : ""} onClick=${() => setHours(h)}>${label}</button>`)}</span>
      <label class="muted small"><input type="checkbox" checked=${direct} onChange=${(e) => setDirect(e.target.checked)} /> Heard direct only</label>
      <label class="muted small">Count
        <select aria-label="Count" value=${count} onChange=${(e) => setCount(e.target.value)}>
          <option value="packets">Packets</option><option value="stations">Stations</option>
        </select></label>
      <label class="muted small">Grid
        <select aria-label="Grid cell" value=${cell} onChange=${(e) => setCell(e.target.value)}>
          ${CELLS.map(([v, label]) => html`<option key=${v} value=${v}>${label}</option>`)}
        </select></label>
    </div>
    <div class="kpis">
      <${Tile} label="Packets gated" value=${num(t.packets)} note=${`in ${win}`} />
      <${Tile} label="With a position" value=${num(t.positions)} note=${`${num(t.direct)} heard direct`} />
      <${Tile} label="Stations" value=${num(t.stations)} note=${`${num(t.direct_stations)} heard direct`} />
      <${Tile} label="Effective range" value=${fit ? len(d.units, fit.effective_km) : "–"}
        note=${fit ? `90% of the fit · half within ${len(d.units, fit.r50_km)}` : "needs more packets"} />
      <${Tile} label="Farthest" value=${len(d.units, dist?.max_km)} note=${dist?.p90_km != null ? `90% heard within ${len(d.units, dist.p90_km)}` : ""} />
    </div>
    ${!d.home && html`<p class="muted">Distances wait for ${d.igate}'s own position: it shows up when it next beacons,
      or set its location under Settings → APRS.</p>`}
    <${IgateMap} d=${d} cell=${cell} sectors=${sectors} />
    ${dist && html`<div class="card">
      <h2>Count by distance</h2>
      <p class="muted small">${num(dist.n)} ${what}${direct ? " heard direct" : ""}, ${len(d.units, dist.bin_km, 1)} bins
        ${dist.beyond ? ` · ${num(dist.beyond)} farther than the last bin (the farthest 1%) aren't drawn but count in the fit` : ""}</p>
      ${dist.n ? html`<${RangeChart} dist=${dist} units=${d.units} count=${count} />` : html`<p class="muted">Nothing with a position in ${win}.</p>`}
      ${fit && html`<p class="muted small">Weibull fit to the cumulative share: shape ${fit.k}, scale ${len(d.units, fit.scale_km)},
        R² ${fit.r2}. Effective range is the distance it puts 90% within; 95% within ${len(d.units, fit.r95_km)}.</p>`}
    </div>`}
    ${sectors && d.home && html`<${Directions} s=${sectors} units=${d.units} n=${n} setN=${setN} />`}
    ${d.farthest?.length > 0 && html`<div class="card">
      <h2>Heard farthest</h2>
      <div class="aprspackets"><table><tbody>
        ${d.farthest.map((f) => html`<tr key=${f.callsign}><td><a href=${`https://aprs.fi/info/a/${encodeURIComponent(f.callsign)}`}
          target="_blank" rel="noopener">${f.callsign}</a></td><td>${len(d.units, f.km)}</td><td class="mono">${grid(f.lat, f.lon)}</td>
          <td class="muted">${when(f.ts)}${f.direct ? "" : " · digipeated"}</td></tr>`)}
      </tbody></table></div>
    </div>`}
    <${Stations} hours=${hours} units=${d.units} />
    <p class="muted small">Counted from when the app started listening${t.first_ts ? ` (${when(t.first_ts)})` : ""}: APRS-IS keeps no history.
      ${d.stream?.state !== "connected" ? ` APRS-IS: ${d.stream?.state || "off"}.` : ""}</p>`;
}
