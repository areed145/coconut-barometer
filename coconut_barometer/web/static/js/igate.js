// APRS → Igate: the packets APRS-IS credits the igate in Settings → APRS with gating. A map counts them on a
// lat/lon grid (color on a log scale, as on the photo map), with the igate and its effective range; a chart counts
// them by distance from the igate, with the Weibull fit whose 90% distance is that effective range (igate.py).
// Distance is from direct packets by default: a digipeated one says where the sender was, not how far the igate hears.
// By direction (#115): the same fit per sector of bearing, as an outline on the map and a polar chart.
// The map can show instead the digipeaters its packets came through (#117), from their paths.
// Then the long-distance log, and range over time beside the station's weather (#116).
// Below, who it heard (#118): stations per day, new and regular, and what kind each one is (from its symbol).
import uPlot from "../vendor/uPlot.esm.js";
import { html, useState, useEffect, useRef, api, ago, toast, every, shade, logShare, num, useThemeTick, signedIn } from "./lib.js";
import { leaflet, baseMap } from "./leaflet.js";
import { grid } from "./aprs.js";
import { useMapTools, mapboxClass } from "./wx_controls.js";
import { Bars, Table } from "./viz.js";
import { Chart } from "./chart.js";
import { unitsFor, compass } from "./units.js";

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

// Sender → digi → … → igate, a line per link (thicker: more packets); digis sized by what they relayed.
function drawNetwork(L, group, d, net, bounds) {
  const at = (call) => (call === d.igate && d.home ? [d.home.lat, d.home.lon] : net.places[call]);
  const top = net.edges.reduce((m, e) => Math.max(m, e[2]), 1);
  for (const [a, b, n] of [...net.edges].reverse()) {
    const p = at(a), q = at(b);
    if (!p || !q) continue;
    L.polyline([p, q], { color: css("--chart-1"), weight: 1.5 + 5 * Math.sqrt(n / top), opacity: 0.55 })
      .bindTooltip(`${a} → ${b}: ${num(n)} packet${n === 1 ? "" : "s"}`).addTo(group);
    bounds.push(p, q);
  }
  const digis = new Set(net.digis.map((x) => x.callsign));
  for (const call of new Set(net.edges.flatMap((e) => [e[0], e[1]]))) {
    if (digis.has(call) || call === d.igate || !net.places[call]) continue;
    L.circleMarker(net.places[call], { radius: 3, color: css("--muted"), weight: 1, fillOpacity: 0.8 }).bindTooltip(call).addTo(group);
  }
  const most = net.digis.reduce((m, x) => Math.max(m, x.packets), 1);
  for (const x of net.digis) {
    if (!x.at) continue;
    const pop = el("div", "wxpop");
    pop.append(el("b", null, x.callsign), el("div", null, `${num(x.packets)} packets relayed · ${(x.share * 100).toFixed(0)}% of all gated`),
      el("div", null, `${num(x.sender_count)} sender${x.sender_count === 1 ? "" : "s"}: ${x.senders.slice(0, 10).join(", ")}${x.sender_count > 10 ? ", …" : ""}`),
      el("div", "muted", `last relayed ${ago(x.last_ts)}`));
    L.circleMarker(x.at, { radius: 5 + 7 * Math.sqrt(x.packets / most), color: css("--fg"), weight: 1.5, fillColor: css("--chart-2"), fillOpacity: 0.9 })
      .bindPopup(pop).addTo(group);
    bounds.push(x.at);
  }
}

function IgateMap({ d, cell, sectors, view, net }) {
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
    if (view === "digis" && net?.edges) drawNetwork(L, group, d, net, bounds);
    // Least first, so the busiest cells draw on top.
    for (const [lat, lon, n, stations] of view === "digis" ? [] : [...rows].reverse()) {
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
      if (view !== "digis" && rows?.some((r) => r.fit)) {
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
    // Fit to the data once per igate and view, and again when the first cells arrive.
    const key = `${d.igate}:${view}:${bounds.length > 1}`;
    if (bounds.length && fitted.current !== key) {
      fitted.current = key;
      map.current.fitBounds(bounds, { padding: [40, 40], maxZoom: 11 });
    }
  }, [ready, d, cell, theme, sectors, view, net]);
  const top = d.cells.rows.reduce((m, r) => Math.max(m, r[2]), 0);
  return html`<div ref=${wrap} class=${mapboxClass(fullscreen, "igatemapbox")}>
    <div class="mapwrap igatemap" ref=${box}></div>
    ${view === "digis" ? html`<div class="igatescale">Lines: packets along each link, thicker for more ·
      <span class="igatedot digi"></span> digipeater (click for its senders) · <span class="igatedot"></span> sender</div>`
    : top > 0 && html`<div class="igatescale" aria-label="Packets per cell">
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

// The digis the igate's packets came through, and how many came direct.
function Digis({ net, units }) {
  if (!net?.hops || !net.total) return null;
  const h = net.hops, share = (n) => `${((n / net.total) * 100).toFixed(0)}%`;
  const rows = [{ label: "Heard direct", n: h.direct }, { label: "Through one digi", n: h.one }, { label: "Through two or more", n: h.more }];
  const cols = [
    { key: "callsign", label: "Digipeater", fmt: (c) => (c === "?" ? html`<span class="muted" title="A WIDEn-N used by a digi that didn't put its call in the path">unknown</span>` : aprsfi(c)) },
    { key: "packets", label: "Relayed", num: true },
    { key: "share", label: "Share", num: true, fmt: (v) => `${(v * 100).toFixed(1)}%` },
    { key: "sender_count", label: "Senders", num: true },
    { key: "last_ts", label: "Last relayed", num: true, fmt: (t) => ago(t) },
    { key: "at", label: "Where", fmt: (at) => (at ? html`<span class="mono">${grid(at[0], at[1])}</span>` : html`<span class="muted">not heard placing itself</span>`) },
  ];
  return html`<div class="card">
    <h2>Digipeaters</h2>
    <p class="muted small">How the ${num(net.total)} packets gated reached the igate, from their paths. Map → Digipeaters draws the links.</p>
    <${Bars} rows=${rows} value="n" right=${(r) => `${num(r.n)} · ${share(r.n)}`} />
    ${net.digis.length ? html`<${Table} rows=${net.digis} cols=${cols} initial="packets" />` : html`<p class="muted">Everything came direct.</p>`}
  </div>`;
}

// The stations heard farthest direct, each at its farthest believable packet, in the window or ever.
function LongLog({ hours, win, units }) {
  const [g, setG] = useState(null);
  const [ever, setEver] = useState(false);
  useEffect(() => {
    let alive = true;
    const load = () => api("/api/aprs/igate/long", { params: { hours } }).then((x) => alive && setG(x)).catch(() => {});
    const stop = every(load, 60000);
    return () => { alive = false; stop(); };
  }, [hours]);
  if (!g?.window) return null;
  const log = ever ? g.ever : g.window, out = log.left_out;
  const cols = [
    { key: "callsign", label: "Station", fmt: (c) => aprsfi(c) },
    { key: "km", label: "Distance", num: true, fmt: (km) => len(units, km) },
    { key: "bearing", label: "Direction", num: true, fmt: (b) => `${compass(b)} · ${Math.round(b) % 360}°` },
    { key: "ts", label: "Heard", num: true, fmt: (t) => when(t) },
    { key: "path", label: "Path", fmt: (p) => html`<span class="mono small">${(p || "").split(",qA")[0] || "direct"}</span>` },
  ];
  return html`<div class="card">
    <h2>Long-distance log</h2>
    <div class="toolbar"><span class="seg" aria-label="Over">${[[false, `In ${win}`], [true, "Ever"]].map(([v, label]) => html`<button
      key=${label} class=${ever === v ? "primary" : ""} onClick=${() => setEver(v)}>${label}</button>`)}</span></div>
    ${log.rows.length ? html`<${Table} rows=${log.rows} cols=${cols} initial="km" />` : html`<p class="muted">Nothing heard direct with a position.</p>`}
    <p class="muted small">Heard direct only, each station at its farthest.${out.implausible || out.moved ? ` Left out as unbelievable:
      ${[out.implausible && `${num(out.implausible)} packet${out.implausible === 1 ? "" : "s"} past 400 km from stations that never sent three that far`,
         out.moved && `${num(out.moved)} from fixed stations more than 50 km from where they now say they are`].filter(Boolean).join("; ")}.` : ""}</p>
  </div>`;
}

const pearson = (pairs) => {
  const n = pairs.length;
  if (n < 3) return null;
  const mx = pairs.reduce((s, p) => s + p[0], 0) / n, my = pairs.reduce((s, p) => s + p[1], 0) / n;
  const sxy = pairs.reduce((s, p) => s + (p[0] - mx) * (p[1] - my), 0);
  const sxx = pairs.reduce((s, p) => s + (p[0] - mx) ** 2, 0), syy = pairs.reduce((s, p) => s + (p[1] - my) ** 2, 0);
  return sxx && syy ? sxy / Math.sqrt(sxx * syy) : null;
};

// A uPlot that sizes itself to its box and redraws on a theme switch. make(width) -> [opts, data].
function Plot({ make, deps, height = 220 }) {
  const box = useRef(null);
  const theme = useThemeTick();
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const [opts, data] = make();
    const plot = new uPlot({ ...opts, width: el.clientWidth, height }, data, el);
    const ro = new ResizeObserver(() => plot.setSize({ width: el.clientWidth, height }));
    ro.observe(el);
    return () => { ro.disconnect(); plot.destroy(); };
  }, [...deps, theme]);
  return html`<div class="chart" ref=${box}></div>`;
}

// Range over time (each day's or hour's own fit) over the station's weather, and how range goes with pressure and
// with how far it cooled overnight. Long-distance days (the top tenth by 99th-percentile distance) stand out.
function RangeWeather({ hours, units }) {
  const [step, setStep] = useState("day");
  const [r, setR] = useState(null);
  const hourly = hours <= 24 * 31;
  const want = hourly ? step : "day";
  useEffect(() => {
    let alive = true;
    const load = () => api("/api/aprs/igate/range", { params: { hours, step: want } }).then((x) => alive && setR(x)).catch(() => {});
    const stop = every(load, 300000);
    return () => { alive = false; stop(); };
  }, [hours, want]);
  if (!r?.t || r.t.length < 2) return null;
  const U = unitsFor(units), k = units === "imperial" ? MI : 1, dl = units === "imperial" ? "mi" : "km";
  const conv = (kind, v) => (v == null ? null : U[kind][1](v));
  const cut = [...r.p99_km].filter((v) => v != null).sort((a, b) => a - b);
  const top = cut.length >= 10 ? cut[Math.floor(cut.length * 0.9)] : Infinity;
  const long = r.p99_km.map((v) => v != null && v >= top);
  const axis = () => ({ stroke: css("--muted"), grid: { stroke: css("--line"), width: 1 }, ticks: { stroke: css("--line") } });
  const sync = { key: "igate-range" };
  const line = (label, color, scale, extra = {}) => ({ label, stroke: css(color), width: 2, scale, spanGaps: false, points: { show: r.t.length < 60 }, ...extra });
  const rangeChart = () => [{
    cursor: { sync }, legend: { live: true }, scales: { x: { time: true }, d: { range: (u, mn, mx) => [0, Math.max(1, mx) * 1.08] }, n: { range: (u, mn, mx) => [0, Math.max(1, mx) * 1.1] } },
    axes: [axis(), { ...axis(), scale: "d", size: 52, label: dl, labelSize: 14 }, { ...axis(), scale: "n", side: 1, size: 44, grid: { show: false } }],
    series: [{}, { label: "Stations heard", stroke: css("--muted"), fill: css("--line"), scale: "n", width: 0, points: { show: false },
      paths: uPlot.paths.bars({ size: [0.6, 30] }) },
      line("Effective range (90%)", "--chart-1", "d"), line("99% heard within", "--chart-2", "d", { dash: [5, 4] })],
    hooks: { drawClear: [(u) => {          // long-distance days: a faint band behind
      const { ctx, bbox } = u, w = Math.max(2, (u.valToPos(r.t[1], "x", true) - u.valToPos(r.t[0], "x", true)) * 0.9);
      ctx.save(); ctx.fillStyle = css("--chart-2") + "22";
      r.t.forEach((t, i) => long[i] && ctx.fillRect(u.valToPos(t, "x", true) - w / 2, bbox.top, w, bbox.height));
      ctx.restore();
    }] },
  }, [r.t, r.stations, r.effective_km.map((v) => v && v * k), r.p99_km.map((v) => v && v * k)]];
  const weatherChart = () => [{
    cursor: { sync }, legend: { live: true }, scales: { x: { time: true }, p: { auto: true }, t: { auto: true } },
    axes: [axis(), { ...axis(), scale: "p", size: 56, label: U.pressure[0], labelSize: 14 }, { ...axis(), scale: "t", side: 1, size: 44, grid: { show: false } }],
    series: [{}, line("Pressure (sea level)", "--chart-3", "p"), line(`Temperature (${U.temp[0]})`, "--chart-1", "t"),
      line(`Dew point (${U.temp[0]})`, "--chart-2", "t", { dash: [5, 4] })],
  }, [r.t, r.pressure.map((v) => conv("pressure", v)), r.temp.map((v) => conv("temp", v)), r.dew_point.map((v) => conv("temp", v))]];
  // x against effective range, ordinary days and long-distance days as two point series.
  const scatter = (xs, xlabel) => () => {
    const pts = r.t.map((_, i) => [xs[i], r.effective_km[i] && r.effective_km[i] * k, long[i]]).filter((p) => p[0] != null && p[1] != null)
      .sort((a, b) => a[0] - b[0]);
    return [{
      mode: 1, legend: { show: false }, cursor: { drag: { x: false, y: false } }, scales: { x: { time: false }, y: { range: (u, mn, mx) => [0, Math.max(1, mx) * 1.08] } },
      axes: [{ ...axis(), label: xlabel, labelSize: 16 }, { ...axis(), size: 48, label: `Effective range (${dl})`, labelSize: 14 }],
      series: [{}, { stroke: css("--chart-1"), paths: () => null, points: { show: true, size: 6, fill: css("--chart-1") } },
        { stroke: css("--chart-2"), paths: () => null, points: { show: true, size: 8, fill: css("--chart-2") } }],
    }, [pts.map((p) => p[0]), pts.map((p) => (p[2] ? null : p[1])), pts.map((p) => (p[2] ? p[1] : null))]];
  };
  const pressures = r.pressure.map((v) => conv("pressure", v));
  const drops = r.overnight_drop?.map((v) => (v == null ? null : units === "imperial" ? v * 1.8 : v));
  const rOf = (xs) => pearson(r.t.map((_, i) => [xs[i], r.effective_km[i]]).filter((p) => p[0] != null && p[1] != null));
  const rp = rOf(pressures), rd = drops && rOf(drops), fitted = r.effective_km.filter((v) => v != null).length;
  const say = (x) => (x == null ? "not enough days with both yet" : `r = ${x.toFixed(2)}`);
  return html`<div class="card">
    <h2>Range over time</h2>
    <div class="toolbar"><span class="seg" aria-label="Per">${[["day", "Per day"], ["hour", "Per hour"]].map(([v, label]) => html`<button key=${v}
      class=${want === v ? "primary" : ""} disabled=${v === "hour" && !hourly} title=${v === "hour" && !hourly ? "For windows up to 31 days" : ""}
      onClick=${() => setStep(v)}>${label}</button>`)}</span></div>
    <p class="muted small">Each ${want}'s effective range is its own fit (${r.min_packets}+ direct packets: ${num(fitted)} of ${num(r.t.length)} have one).
      More stations sending means more chances to hear a far one, so they're drawn too.${long.some(Boolean) ? " Shaded: long-distance " + want + "s (the top tenth by 99% distance)." : ""}</p>
    <${Plot} make=${rangeChart} deps=${[r, units]} />
    ${r.station ? html`<h3>Weather at ${r.station}</h3><${Plot} make=${weatherChart} deps=${[r, units]} height=${180} />`
      : html`<p class="muted">No weather station to set beside it.</p>`}
    ${r.station && html`<div class="igatescatter">
      <div><h3>Range against pressure</h3><${Plot} make=${scatter(pressures, `Pressure (${U.pressure[0]})`)} deps=${[r, units]} height=${200} />
        <p class="muted small">${say(rp)}. VHF tends to carry farther under high pressure.</p></div>
      ${drops && html`<div><h3>Range against the overnight cooling</h3><${Plot} make=${scatter(drops, `Fell overnight (${U.temp[0]})`)} deps=${[r, units]} height=${200} />
        <p class="muted small">${say(rd)}. Warmest of the evening before less coolest of the morning: clear, calm nights
          cool most, and can bend VHF farther.</p></div>`}
    </div>`}
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
  const [view, setView] = useState("heat");
  const [net, setNet] = useState(null);
  useEffect(() => {
    let alive = true;
    const load = () => api("/api/aprs/igate/digis", { params: { hours } }).then((x) => alive && setNet(x)).catch(() => {});
    const stop = every(load, 60000);
    return () => { alive = false; stop(); };
  }, [hours]);
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
      <label class="muted small">Map
        <select aria-label="Map" value=${view} onChange=${(e) => setView(e.target.value)}>
          <option value="heat">Heat map</option><option value="digis">Digipeaters</option>
        </select></label>
      ${view === "heat" && html`<label class="muted small">Grid
        <select aria-label="Grid cell" value=${cell} onChange=${(e) => setCell(e.target.value)}>
          ${CELLS.map(([v, label]) => html`<option key=${v} value=${v}>${label}</option>`)}
        </select></label>`}
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
    <${IgateMap} d=${d} cell=${cell} sectors=${sectors} view=${view} net=${net} />
    ${dist && html`<div class="card">
      <h2>Count by distance</h2>
      <p class="muted small">${num(dist.n)} ${what}${direct ? " heard direct" : ""}, ${len(d.units, dist.bin_km, 1)} bins
        ${dist.beyond ? ` · ${num(dist.beyond)} farther than the last bin (the farthest 1%) aren't drawn but count in the fit` : ""}</p>
      ${dist.n ? html`<${RangeChart} dist=${dist} units=${d.units} count=${count} />` : html`<p class="muted">Nothing with a position in ${win}.</p>`}
      ${fit && html`<p class="muted small">Weibull fit to the cumulative share: shape ${fit.k}, scale ${len(d.units, fit.scale_km)},
        R² ${fit.r2}. Effective range is the distance it puts 90% within; 95% within ${len(d.units, fit.r95_km)}.</p>`}
    </div>`}
    ${sectors && d.home && html`<${Directions} s=${sectors} units=${d.units} n=${n} setN=${setN} />`}
    <${LongLog} hours=${hours} win=${win} units=${d.units} />
    <${RangeWeather} hours=${hours} units=${d.units} />
    <${Digis} net=${net} units=${d.units} />
    <${Stations} hours=${hours} units=${d.units} />
    <p class="muted small">Counted from when the app started listening${t.first_ts ? ` (${when(t.first_ts)})` : ""}: APRS-IS keeps no history.
      ${d.stream?.state !== "connected" ? ` APRS-IS: ${d.stream?.state || "off"}.` : ""}</p>`;
}
