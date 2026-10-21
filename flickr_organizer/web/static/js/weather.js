// The Weather section: Live (the stations now), History (charts), Stations (what's stored, sync).
// Everything is stored metric; the unit setting (Settings → Weather) only changes how it's shown here.
import uPlot from "../vendor/uPlot.esm.js";
import { html, useState, useEffect, useRef, useMemo, api, num, ago, toast, useThemeTick, shade, logShare } from "./lib.js";

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ---- units ------------------------------------------------------------------------------
const UNITS = {
  metric: { temp: ["°C", (v) => v], speed: ["m/s", (v) => v], pressure: ["hPa", (v) => v], rain: ["mm", (v) => v], dist: ["km", (v) => v] },
  imperial: { temp: ["°F", (v) => v * 1.8 + 32], speed: ["mph", (v) => v * 2.23694], pressure: ["inHg", (v) => v * 0.02953],
              rain: ["in", (v) => v / 25.4], dist: ["mi", (v) => v * 0.621371] },
};
const unitsFor = (system) => UNITS[system] || UNITS.metric;
const fmt = (v, digits = 1) => (v == null ? "–" : Number(v).toFixed(digits));
const show = (u, kind, v, digits = 1) => (v == null ? "–" : `${fmt(unitsFor(u)[kind][1](v), digits)} ${unitsFor(u)[kind][0]}`);
const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
const compass = (deg) => (deg == null ? "" : COMPASS[Math.round((deg % 360) / 22.5) % 16]);
const when = (ts) => new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

// ---- Live -------------------------------------------------------------------------------
function WindDial({ dir, speed, unit }) {
  return html`<svg class="dial" viewBox="-50 -50 100 100" role="img" aria-label=${`Wind from ${compass(dir) || "unknown"}`}>
    <circle r="44" fill="none" stroke="var(--line)" stroke-width="1.5" />
    ${[["N", 0, -34], ["E", 34, 0], ["S", 0, 34], ["W", -34, 0]].map(([l, x, y]) =>
      html`<text x=${x} y=${y} text-anchor="middle" dominant-baseline="central" font-size="8" fill="var(--muted)">${l}</text>`)}
    ${dir != null && html`<g transform=${`rotate(${(dir + 180) % 360})`}><path d="M0,-30 L7,-12 L0,-17 L-7,-12 Z" fill="var(--accent)" /></g>`}
    <text y="2" text-anchor="middle" font-size="15" font-weight="600" fill="var(--fg)">${speed == null ? "–" : fmt(unitsFor(unit).speed[1](speed), speed * 2.24 >= 10 ? 0 : 1)}</text>
    <text y="14" text-anchor="middle" font-size="7" fill="var(--muted)">${unitsFor(unit).speed[0]}</text>
  </svg>`;
}

function Stat({ label, children, note }) {
  return html`<div class="kpi"><span>${label}</span><b>${children}</b>${note && html`<small> ${note}</small>`}</div>`;
}

function StationCard({ s, rapid, unit, now }) {
  const o = s.obs;
  if (!o) return html`<div class="card"><h2>${s.name}</h2><p class="muted">No observations stored yet. Syncing, or waiting for the first report.</p></div>`;
  const live = rapid && now - rapid.ts < 30 ? rapid : null;      // the 3-second wind, while it's flowing
  const dir = live ? live.dir : o.wind_dir, speed = live ? live.speed : o.wind_avg;
  const trend = o.pressure_change_3h;
  const stale = now - o.ts > 600;
  return html`<div class=${"card wxcard" + (stale ? " stale" : "")}>
    <h2>${s.name} <span class="muted">${stale ? `last report ${ago(o.ts)}` : live ? "live" : ago(o.ts)}</span></h2>
    <div class="wxmain">
      <div class="wxtemp"><b>${show(unit, "temp", o.temp)}</b>
        <span class="muted">${show(unit, "temp", o.temp_low, 0)} – ${show(unit, "temp", o.temp_high, 0)} today</span></div>
      <${WindDial} dir=${dir} speed=${speed} unit=${unit} />
    </div>
    <div class="kpis small">
      <${Stat} label="Humidity">${fmt(o.humidity, 0)}%<//>
      <${Stat} label="Pressure" note=${trend == null ? "" : `${trend >= 0 ? "▲" : "▼"} ${fmt(Math.abs(unitsFor(unit).pressure[1](trend)), unit === "imperial" ? 2 : 1)} / 3 h`}>${show(unit, "pressure", o.pressure_sl, unit === "imperial" ? 2 : 1)}<//>
      <${Stat} label="Wind" note=${compass(dir)}>${show(unit, "speed", o.wind_avg)}<//>
      <${Stat} label="Gust" note=${o.gust_high != null ? `${show(unit, "speed", o.gust_high, 0)} today` : ""}>${show(unit, "speed", o.wind_gust)}<//>
      <${Stat} label="Rain today">${show(unit, "rain", o.rain_today, unit === "imperial" ? 2 : 1)}<//>
      <${Stat} label="UV">${fmt(o.uv, 1)}<//>
      <${Stat} label="Solar">${fmt(o.solar, 0)} W/m²<//>
      <${Stat} label="Lightning" note=${s.obs.last_strike ? `${show(unit, "dist", s.obs.last_strike.distance, 0)} away` : ""}>${s.obs.last_strike ? ago(s.obs.last_strike.ts) : "none"}<//>
    </div>
  </div>`;
}

function StreamBadge({ stream }) {
  if (!stream) return "";
  const ok = stream.state === "connected";
  return html`<span class=${"status " + (ok ? "s-applied" : "s-failed")} title=${stream.error || ""}>${ok ? "live stream connected" : "stream " + stream.state}</span>`;
}

function Live() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    const load = () => api("/api/wx/live").then((x) => { if (live) { setD(x); setErr(""); } }).catch((e) => live && setErr(e.message));
    load();
    const t = setInterval(load, 3000);
    return () => { live = false; clearInterval(t); };
  }, []);
  if (!d) return err ? html`<p class="problem">${err}</p>` : html`<div class="boot">Loading…</div>`;
  if (!d.stations.length) return html`<${Empty} />`;
  return html`
    <p class="muted"><${StreamBadge} stream=${d.stream} />
      ${d.stream.last_message ? ` last message ${ago(d.stream.last_message)}` : ""}</p>
    <div class="cols wxcols">${d.stations.map((s) => html`<${StationCard} key=${s.device_id} s=${s}
      rapid=${d.rapid[String(s.device_id)]} unit=${d.units} now=${d.now} />`)}</div>
    ${d.events.length > 0 && html`<div class="card"><h2>Recent events</h2>
      ${d.events.slice().reverse().map((e, i) => html`<div key=${i} class="muted">${when(e.ts)} · ${e.kind === "strike"
        ? `lightning${e.distance != null ? `, ${show(d.units, "dist", e.distance, 0)} away` : ""}` : "rain started"}</div>`)}</div>`}`;
}

function Empty() {
  return html`<div class="card"><h2>No stations yet</h2>
    <p>Add your WeatherFlow token under <a href="#/settings">Settings → API keys</a>. The stations on that account are found
    on the next sync, and their history appears under <a href="#/queue">Queue</a> as it's fetched.</p></div>`;
}

// ---- History ----------------------------------------------------------------------------
// Local dates (YYYY-MM-DD) -> the whole days from midnight of `from` to the end of `to` (or now, if that is today).
const day = (offset) => { const d = new Date(); d.setDate(d.getDate() + offset); return d.toLocaleDateString("sv"); };
const pickRange = (from, to) => ({ from, to, range: [
  Math.floor(new Date(from + "T00:00:00").getTime() / 1000),
  Math.min(Math.floor(Date.now() / 1000), Math.floor(new Date(to + "T23:59:59").getTime() / 1000))] });
const RANGES = [["24h", 24], ["7d", 24 * 7], ["30d", 24 * 30], ["90d", 24 * 90], ["1y", 24 * 365]];

function TimeChart({ t, series, height = 200, unit = "", bars = false }) {
  const box = useRef(null);
  const theme = useThemeTick();
  useEffect(() => {
    const el = box.current;
    if (!el || !t.length) return;
    const axis = { stroke: css("--muted"), grid: { stroke: css("--line"), width: 1 }, ticks: { stroke: css("--line") } };
    const plot = new uPlot({
      width: el.clientWidth, height, cursor: { points: { size: 6 } }, legend: { live: true },
      scales: { x: { time: true } },
      axes: [axis, { ...axis, size: 52, label: unit, labelSize: 14, labelFont: "11px sans-serif" }],
      series: [{}, ...series.map((s) => ({
        label: s.label, stroke: css(s.color), width: s.thin ? 1 : 2, dash: s.dash, points: { show: false },
        fill: bars ? css(s.color) + "aa" : undefined, paths: bars ? uPlot.paths.bars({ size: [0.8, 30] }) : undefined,
        value: (u, v) => (v == null ? "–" : v.toFixed(s.digits ?? 1)),
      }))],
    }, [t, ...series.map((s) => s.values)], el);
    const ro = new ResizeObserver(() => plot.setSize({ width: el.clientWidth, height }));
    ro.observe(el);
    return () => { ro.disconnect(); plot.destroy(); };
  }, [t, series, height, unit, bars, theme]);
  return html`<div class="chart" ref=${box}></div>`;
}

// Strikes by time (30-minute bins, the same span as the other charts) and distance, colored along the
// Settings → Colors gradient. Distance bands: 5 km, or 3 mi when the stations are shown in miles.
function Lightning({ device, start, end, unit }) {
  const [d, setD] = useState(null);
  const box = useRef(null);
  const theme = useThemeTick();
  useEffect(() => {
    let live = true;
    setD(null);
    api(`/api/wx/lightning?device=${device}&start=${start}&end=${end}`).then((x) => live && setD(x)).catch(() => live && setD({ cells: [], total: 0 }));
    return () => { live = false; };
  }, [device, start, end]);
  useEffect(() => {
    const el = box.current;
    if (!el || !d || !d.total) return;
    const [label, conv] = unitsFor(unit).dist, size = unit === "imperial" ? 3 : 5, count = unit === "imperial" ? 9 : 8;
    const cells = new Map();
    for (const [t, km, n] of d.cells) {
      const b = Math.min(count - 1, Math.floor(conv(km) / size)), k = t + ":" + b;
      cells.set(k, (cells.get(k) || 0) + n);
    }
    const top = Math.max(...cells.values());
    const edges = Array.from({ length: count + 1 }, (_, i) => i * size);
    const axis = { stroke: css("--muted"), grid: { stroke: css("--line"), width: 1 }, ticks: { stroke: css("--line") } };
    const paint = (u) => {
      const { ctx, bbox } = u;
      ctx.save(); ctx.beginPath(); ctx.rect(bbox.left, bbox.top, bbox.width, bbox.height); ctx.clip();
      for (const [k, n] of cells) {
        const [t, b] = k.split(":").map(Number);
        const x0 = u.valToPos(t, "x", true), x1 = u.valToPos(t + d.step, "x", true);
        const y0 = u.valToPos(edges[b + 1], "y", true), y1 = u.valToPos(edges[b], "y", true);
        ctx.fillStyle = shade(logShare(n, top));
        ctx.fillRect(x0, y0, Math.max(1, x1 - x0), y1 - y0);
      }
      ctx.restore();
    };
    const plot = new uPlot({
      width: el.clientWidth, height: 200, cursor: { show: false }, legend: { show: false },
      scales: { x: { time: true }, y: { range: [0, count * size] } },
      axes: [axis, { ...axis, size: 52, label: label, labelSize: 14, labelFont: "11px sans-serif", splits: () => edges }],
      series: [{}, { points: { show: false } }],
      hooks: { draw: [paint] },
    }, [[d.start, d.end], [null, null]], el);
    const ro = new ResizeObserver(() => plot.setSize({ width: el.clientWidth, height: 200 }));
    ro.observe(el);
    return () => { ro.disconnect(); plot.destroy(); };
  }, [d, unit, theme]);
  if (!d) return html`<div class="boot">Loading…</div>`;
  if (!d.total) return html`<p class="muted">No lightning in this range.</p>`;
  return html`<div class="chart" ref=${box}></div>
    <p class="muted heatkey"><span>${num(d.total)} strikes · fewer</span><i style="background: linear-gradient(90deg, ${shade(0)}, ${shade(1)})"></i><span>more per 30 min</span></p>`;
}

function Rose({ device, start, end, unit }) {
  const [r, setR] = useState(null);
  useEffect(() => { api(`/api/wx/rose?device=${device}&start=${start}&end=${end}`).then(setR).catch(() => setR(null)); }, [device, start, end]);
  if (!r || !r.total) return html`<p class="muted">No wind data for this range.</p>`;
  const max = Math.max(...r.counts.map((c) => c.reduce((a, b) => a + b, 0)));
  const tone = (j) => shade(r.bands.length > 1 ? j / (r.bands.length - 1) : 1);   // the Settings → Colors gradient, calm to strong
  const R = 44, step = 22.5, gap = 1.5;
  const arc = (i, r0, r1) => {
    const a0 = ((i * step - step / 2 + gap) - 90) * Math.PI / 180, a1 = ((i * step + step / 2 - gap) - 90) * Math.PI / 180;
    const p = (rad, a) => `${(rad * Math.cos(a)).toFixed(2)},${(rad * Math.sin(a)).toFixed(2)}`;
    return `M${p(r0, a0)} L${p(r1, a0)} A${r1},${r1} 0 0 1 ${p(r1, a1)} L${p(r0, a1)} A${r0},${r0} 0 0 0 ${p(r0, a0)} Z`;
  };
  const bandLabel = (j) => `${fmt(unitsFor(unit).speed[1](r.bands[j]), 0)}${j + 1 < r.bands.length ? "–" + fmt(unitsFor(unit).speed[1](r.bands[j + 1]), 0) : "+"} ${unitsFor(unit).speed[0]}`;
  return html`<div class="rose"><svg viewBox="-56 -56 112 112" role="img" aria-label="Wind rose">
    <circle r=${R} fill="none" stroke="var(--line)" /><circle r=${R / 2} fill="none" stroke="var(--line)" stroke-dasharray="2 3" />
    ${[["N", 0, -52], ["E", 52, 0], ["S", 0, 52], ["W", -52, 0]].map(([l, x, y]) =>
      html`<text x=${x} y=${y} text-anchor="middle" dominant-baseline="central" font-size="6" fill="var(--muted)">${l}</text>`)}
    ${r.counts.map((c, i) => { let cum = 0; return c.map((n, j) => { const r0 = (cum / max) * R; cum += n;
      return n ? html`<path key=${i + "-" + j} d=${arc(i, r0, (cum / max) * R)} fill=${tone(j)} />` : ""; }); })}
  </svg>
  <div class="rose-key">${r.bands.map((_, j) => html`<span key=${j}><i style=${`background: ${tone(j)}`}></i>${bandLabel(j)}</span>`)}</div></div>`;
}

// The view is in the address, so it can be linked: #/weather/history?range=30d&station=123 or ?from=2026-10-01&to=2026-10-07.
function History({ params = {} }) {
  const [info, setInfo] = useState(null);
  const isDate = (v) => /^\d{4}-\d{2}-\d{2}$/.test(v || "");
  const hours = (RANGES.find(([l]) => l === params.range) || RANGES[1])[1];
  const custom = isDate(params.from) && isDate(params.to) ? pickRange(params.from, params.to) : null;
  const pickedId = Number(params.station) || null;
  const picked = pickedId && info?.stations.some((s) => s.device_id === pickedId) ? [pickedId] : null;   // null = all
  const open = (next) => {
    const q = new URLSearchParams(Object.entries({ range: params.range, from: params.from, to: params.to, station: params.station, ...next })
      .filter(([k, v]) => v && !(k === "range" && v === RANGES[1][0]))).toString();
    location.hash = "#/weather/history" + (q ? "?" + q : "");
  };
  const setRange = (label) => open({ range: label, from: "", to: "" });
  const setDates = (from, to) => open({ range: "", from, to });
  const setPicked = (ids) => open({ station: ids ? String(ids[0]) : "" });
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => { api("/api/wx/stations").then(setInfo).catch((e) => setErr(e.message)); }, []);
  const devices = info ? info.stations.map((s) => s.device_id) : [];
  const shown = picked || devices;
  const key = shown.join(",");
  useEffect(() => {
    if (!shown.length) return;
    let live = true;
    setData(null);
    const span = custom ? `start=${custom.range[0]}&end=${custom.range[1]}` : `hours=${hours}`;
    api(`/api/wx/history?${shown.map((d) => "device=" + d).join("&")}&${span}`).then((x) => live && setData(x)).catch((e) => live && setErr(e.message));
    return () => { live = false; };
  }, [key, hours, custom && custom.range.join()]);
  const names = useMemo(() => Object.fromEntries((info?.stations || []).map((s) => [s.device_id, s.name])), [info]);
  if (err) return html`<p class="problem">${err}</p>`;
  if (!info) return html`<div class="boot">Loading…</div>`;
  if (!info.stations.length) return html`<${Empty} />`;
  const u = info.units;
  const make = (field, kind, opts = {}) => shown.flatMap((id, i) => data?.devices[id] ? [{
    label: shown.length > 1 ? `${names[id]}${opts.suffix || ""}` : (opts.suffix ? opts.suffix.trim() : names[id]),
    color: `--chart-${i + 1}`, dash: opts.dash, thin: opts.thin, digits: opts.digits,
    values: data.devices[id][opts.from || field].map((v) => (v == null ? null : kind ? unitsFor(u)[kind][1](v) : v)),
  }] : []);
  const chart = (title, series, unit, extra = {}) => html`<div class="card"><h2>${title}</h2>
    <${TimeChart} t=${data.t} series=${series} unit=${unit} ...${extra} /></div>`;
  return html`
    <div class="toolbar">
      <span class="range">${RANGES.map(([label, h]) => html`<button key=${label} class=${!custom && hours === h ? "primary" : ""} onClick=${() => setRange(label)}>${label}</button>`)}
        <button class=${custom ? "primary" : ""} onClick=${() => !custom && setDates(day(-7), day(0))}>Custom</button></span>
      ${custom && html`<span class="daterange">
        <input type="date" aria-label="From" value=${custom.from} max=${custom.to} onChange=${(e) => e.target.value && setDates(e.target.value, custom.to)} />
        <span class="muted">to</span>
        <input type="date" aria-label="To" value=${custom.to} min=${custom.from} max=${day(0)} onChange=${(e) => e.target.value && setDates(custom.from, e.target.value)} />
      </span>`}
      ${info.stations.length > 1 && html`<span class="seg">
        <button class=${!picked ? "primary" : ""} onClick=${() => setPicked(null)}>Both</button>
        ${info.stations.map((s) => html`<button key=${s.device_id} class=${picked && picked.length === 1 && picked[0] === s.device_id ? "primary" : ""}
          onClick=${() => setPicked([s.device_id])}>${s.name}</button>`)}</span>`}
    </div>
    ${!data ? html`<div class="boot">Loading…</div>` : !data.t.length ? html`<p class="muted">Nothing stored for this range yet.</p>` : html`
      <p class="muted">${num(data.t.length)} points, one per ${data.bucket >= 3600 ? `${data.bucket / 3600} h` : `${data.bucket / 60} min`}.</p>
      ${chart("Temperature", make("temp", "temp"), unitsFor(u).temp[0])}
      ${chart("Humidity", make("humidity", null, { digits: 0 }), "%")}
      ${chart("Pressure (sea level)", make("pressure", "pressure", { digits: u === "imperial" ? 2 : 1 }), unitsFor(u).pressure[0])}
      ${chart("Wind", [...make("wind_avg", "speed", { suffix: " avg" }), ...make("wind_gust", "speed", { suffix: " gust", dash: [4, 4], thin: true })], unitsFor(u).speed[0])}
      ${chart("Rain", make("rain", "rain", { digits: u === "imperial" ? 2 : 1 }), `${unitsFor(u).rain[0]} per point`, { bars: true })}
      ${shown.map((id) => html`<div class="card" key=${id}><h2>Lightning${shown.length > 1 ? ` · ${names[id]}` : ""}</h2>
        <${Lightning} device=${id} start=${data.start} end=${data.end} unit=${u} /></div>`)}
      ${chart("Solar radiation", make("solar", null, { digits: 0 }), "W/m²")}
      <div class="rose-grid">${shown.map((id) => html`<div class="card" key=${id}><h2>Wind rose${shown.length > 1 ? ` · ${names[id]}` : ""}</h2>
        <${Rose} device=${id} start=${data.start} end=${data.end} unit=${u} /></div>`)}</div>`}`;
}

// ---- Stations ---------------------------------------------------------------------------
function Stations() {
  const [info, setInfo] = useState(null);
  const load = () => api("/api/wx/stations").then(setInfo).catch(() => {});
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, []);
  const sync = async () => {
    try {
      const r = await api("/api/wx/sync", { method: "POST" });
      toast(r.status === "queued" ? "WeatherFlow sync queued." : r.status === "busy" ? "A sync is already queued." : "Add the WeatherFlow token in Settings first.");
    } catch (e) { toast(e.message); }
  };
  if (!info) return html`<div class="boot">Loading…</div>`;
  return html`
    <div class="card"><h2>Stations</h2>
      ${!info.configured ? html`<${Empty} />` : html`
        <p class="muted">History comes from WeatherFlow's REST API, ${info.backfill_days} day${info.backfill_days === 1 ? "" : "s"} back
          (<a href="#/settings">Settings → Weather</a>); the live feed from its WebSocket. Both appear under <a href="#/queue">Queue</a>.
          <${StreamBadge} stream=${info.stream} /></p>
        ${info.stations.length ? html`<table class="qdone"><thead><tr><th>Station</th><th>Device</th><th>Place</th><th>History</th><th>Observations</th></tr></thead><tbody>
          ${info.stations.map((s) => html`<tr key=${s.device_id}><td>${s.name}</td><td class="muted">${s.serial || s.device_id}</td>
            <td class="muted">${fmt(s.lat, 3)}, ${fmt(s.lon, 3)} · ${fmt(s.elevation, 0)} m</td>
            <td>${s.first_ts ? `${when(s.first_ts)} → ${when(s.last_ts)}` : "none yet"}</td><td>${num(s.observations)}</td></tr>`)}
        </tbody></table>` : html`<p class="muted">No Tempest found on this token yet.</p>`}
        <p><button onClick=${sync}>Sync now</button></p>`}
    </div>`;
}

export const WEATHER_TABS = [["live", "Live"], ["history", "History"], ["stations", "Stations"]];

export function Weather({ tab = "live", params = {} }) {
  return tab === "history" ? html`<${History} params=${params} />` : tab === "stations" ? html`<${Stations} />` : html`<${Live} />`;
}
