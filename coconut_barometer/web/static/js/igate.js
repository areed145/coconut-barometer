// APRS → Igate: the packets APRS-IS credits the igate in Settings → APRS with gating. A map counts them on a
// lat/lon grid (color on a log scale, as on the photo map), with the igate and its effective range; a chart counts
// them by distance from the igate, with the Weibull fit whose 90% distance is that effective range (igate.py).
// Distance is from direct packets by default: a digipeated one says where the sender was, not how far the igate hears.
import uPlot from "../vendor/uPlot.esm.js";
import { html, useState, useEffect, useRef, api, ago, toast, every, shade, logShare, num, useThemeTick, signedIn } from "./lib.js";
import { leaflet, baseMap } from "./leaflet.js";
import { grid } from "./aprs.js";

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

function IgateMap({ d, cell }) {
  const box = useRef(null), map = useRef(null), layer = useRef(null), fitted = useRef("");
  const [ready, setReady] = useState(false);
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
  }, [ready, d, cell, theme]);
  const top = d.cells.rows.reduce((m, r) => Math.max(m, r[2]), 0);
  return html`<div class="igatemapbox">
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

const Tile = ({ label, value, note }) => html`<div class="kpi"><span>${label}</span><b>${value}</b>${note && html`<small> ${note}</small>`}</div>`;

export function Igate() {
  const [hours, setHours] = useState(24 * 30);
  const [direct, setDirect] = useState(true);
  const [count, setCount] = useState("packets");
  const [cell, setCell] = useState("0.1");
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
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
    <${IgateMap} d=${d} cell=${cell} />
    ${dist && html`<div class="card">
      <h2>Count by distance</h2>
      <p class="muted small">${num(dist.n)} ${what}${direct ? " heard direct" : ""}, ${len(d.units, dist.bin_km, 1)} bins
        ${dist.beyond ? ` · ${num(dist.beyond)} farther than the last bin (the farthest 1%) aren't drawn but count in the fit` : ""}</p>
      ${dist.n ? html`<${RangeChart} dist=${dist} units=${d.units} count=${count} />` : html`<p class="muted">Nothing with a position in ${win}.</p>`}
      ${fit && html`<p class="muted small">Weibull fit to the cumulative share: shape ${fit.k}, scale ${len(d.units, fit.scale_km)},
        R² ${fit.r2}. Effective range is the distance it puts 90% within; 95% within ${len(d.units, fit.r95_km)}.</p>`}
    </div>`}
    ${d.farthest?.length > 0 && html`<div class="card">
      <h2>Heard farthest</h2>
      <div class="aprspackets"><table><tbody>
        ${d.farthest.map((f) => html`<tr key=${f.callsign}><td><a href=${`https://aprs.fi/info/a/${encodeURIComponent(f.callsign)}`}
          target="_blank" rel="noopener">${f.callsign}</a></td><td>${len(d.units, f.km)}</td><td class="mono">${grid(f.lat, f.lon)}</td>
          <td class="muted">${when(f.ts)}${f.direct ? "" : " · digipeated"}</td></tr>`)}
      </tbody></table></div>
    </div>`}
    <p class="muted small">Counted from when the app started listening${t.first_ts ? ` (${when(t.first_ts)})` : ""}: APRS-IS keeps no history.
      ${d.stream?.state !== "connected" ? ` APRS-IS: ${d.stream?.state || "off"}.` : ""}</p>`;
}
