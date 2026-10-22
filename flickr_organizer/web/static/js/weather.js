// The Weather section: Live (the stations now), History (charts), Stations (what's stored, sync).
// Everything is stored metric; the unit setting (Settings → Weather) only changes how it's shown here.
import uPlot from "../vendor/uPlot.esm.js";
import { html, useState, useEffect, useRef, useMemo, api, num, ago, toast, useThemeTick, shade, logShare } from "./lib.js";

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ---- units ------------------------------------------------------------------------------
const UNITS = {
  metric: { temp: ["°C", (v) => v], speed: ["m/s", (v) => v], pressure: ["hPa", (v) => v], rain: ["mm", (v) => v], dist: ["km", (v) => v], height: ["m", (v) => v] },
  imperial: { temp: ["°F", (v) => v * 1.8 + 32], speed: ["mph", (v) => v * 2.23694], pressure: ["inHg", (v) => v * 0.02953],
              rain: ["in", (v) => v / 25.4], dist: ["mi", (v) => v * 0.621371], height: ["ft", (v) => v * 3.28084] },
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

// `marks`: [{values, color}] shades the background of every point whose value is truthy, `step` seconds wide.
function TimeChart({ t, series, height = 200, unit = "", bars = false, marks = [], step = 0 }) {
  const box = useRef(null);
  const theme = useThemeTick();
  useEffect(() => {
    const el = box.current;
    if (!el || !t.length) return;
    const axis = { stroke: css("--muted"), grid: { stroke: css("--line"), width: 1 }, ticks: { stroke: css("--line") } };
    const paintMarks = (u) => {
      const { ctx, bbox } = u;
      for (const m of marks) {
        ctx.fillStyle = css(m.color) + "40";
        m.values.forEach((on, i) => {
          if (!on) return;
          const x0 = u.valToPos(t[i], "x", true), x1 = u.valToPos(t[i] + step, "x", true);
          ctx.fillRect(x0, bbox.top, Math.max(1, x1 - x0), bbox.height);
        });
      }
    };
    const plot = new uPlot({
      width: el.clientWidth, height, cursor: { points: { size: 6 } }, legend: { live: true },
      scales: { x: { time: true } }, hooks: { drawClear: [paintMarks] },
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
  }, [t, series, height, unit, bars, marks, step, theme]);
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

// One cell per local day, weeks across and weekdays down, colored by the picked daily value.
const CAL = { high: ["High", "temp"], low: ["Low", "temp"], rain: ["Rain", "rain"], uv: ["Max UV", null] };
function Calendar({ days, unit }) {
  const [metric, setMetric] = useState("high");
  const [title, kind] = CAL[metric];
  const [label, conv] = kind ? unitsFor(unit)[kind] : ["", (v) => v];
  const digits = metric === "rain" ? (unit === "imperial" ? 2 : 1) : metric === "uv" ? 1 : 0;
  const cells = days.map((x) => ({ date: x.date, v: x[metric] == null ? null : conv(x[metric]) }));
  const seen = cells.filter((c) => c.v != null).map((c) => c.v);
  const lo = metric === "high" || metric === "low" ? Math.min(...seen) : 0, hi = Math.max(...seen);
  const tone = (v) => metric === "rain" ? (v > 0 ? shade(logShare(v, hi)) : "var(--line)") : shade(hi > lo ? (v - lo) / (hi - lo) : 1);
  const at = (date) => new Date(date + "T12:00:00");
  const first = at(days[0].date), lead = (first.getDay() + 6) % 7;       // weeks start on Monday
  const pos = (date) => { const i = Math.round((at(date) - first) / 86400000) + lead; return [Math.floor(i / 7), i % 7]; };
  const P = 12, L = 18, T = 12, cols = pos(days[days.length - 1].date)[0] + 1;
  let lastLabel = -9;
  const months = days.flatMap((x) => {
    const [c] = pos(x.date);
    if (!(x === days[0] || x.date.endsWith("-01")) || c - lastLabel < 3) return [];
    lastLabel = c;
    return [html`<text key=${x.date} x=${L + c * P} y="8" font-size="8" fill="var(--muted)">${at(x.date).toLocaleDateString(undefined, { month: "short" })}</text>`];
  });
  return html`
    <div class="seg">${Object.entries(CAL).map(([k, [l]]) => html`<button key=${k} class=${k === metric ? "primary" : ""} onClick=${() => setMetric(k)}>${l}</button>`)}</div>
    ${!seen.length ? html`<p class="muted">No ${title.toLowerCase()} values in this range.</p>` : html`
      <svg class="calendar" viewBox=${`0 0 ${L + cols * P} ${T + 7 * P}`} width=${(L + cols * P) * 1.5} role="img" aria-label=${`${title} by day`}>
        ${months}
        ${[["M", 0], ["W", 2], ["F", 4]].map(([l, r]) => html`<text key=${l} x="0" y=${T + r * P + 8} font-size="8" fill="var(--muted)">${l}</text>`)}
        ${cells.map((c) => { if (c.v == null) return ""; const [col, row] = pos(c.date);
          return html`<rect key=${c.date} x=${L + col * P} y=${T + row * P} width=${P - 2} height=${P - 2} rx="2" fill=${tone(c.v)}>
            <title>${c.date} · ${fmt(c.v, digits)}${label ? " " + label : ""}</title></rect>`; })}
      </svg>
      <p class="muted heatkey"><span>${fmt(lo, digits)}</span><i style=${`background: linear-gradient(90deg, ${shade(0)}, ${shade(1)})`}></i>
        <span>${fmt(hi, digits)}${label ? " " + label : ""}${metric === "rain" ? " · grey: dry" : ""}</span></p>`}`;
}

// Temperature by local day at the station: a low-to-high bar per day (mean marked), a heatmap of the hourly
// means (days across, hours down), the average day, and a calendar. Colored along the Settings → Colors gradient.
function Days({ device, name, start, end, unit }) {
  const [d, setD] = useState(null);
  const band = useRef(null), heat = useRef(null), avg = useRef(null), readout = useRef(null);
  const theme = useThemeTick();
  useEffect(() => {
    let live = true;
    setD(null);
    api(`/api/wx/days?device=${device}&start=${start}&end=${end}`).then((x) => live && setD(x)).catch(() => live && setD({ days: [] }));
    return () => { live = false; };
  }, [device, start, end]);
  const [label, conv] = unitsFor(unit).temp;
  useEffect(() => {
    if (!d || !band.current || !heat.current || !avg.current) return;
    const days = d.days.filter((x) => x.low != null).map((x) => ({ ...x, low: conv(x.low), high: conv(x.high), mean: conv(x.mean),
                                      hours: x.hours.map((v) => (v == null ? null : conv(v))) }));
    const axis = { stroke: css("--muted"), grid: { stroke: css("--line"), width: 1 }, ticks: { stroke: css("--line") } };
    const yAxis = { ...axis, size: 52, labelSize: 14, labelFont: "11px sans-serif" };
    const span = [days[0].t, days[days.length - 1].t + 86400];
    const clip = (u, ctx) => { ctx.save(); ctx.beginPath(); ctx.rect(u.bbox.left, u.bbox.top, u.bbox.width, u.bbox.height); ctx.clip(); };

    const lo = Math.min(...days.map((x) => x.low)), hi = Math.max(...days.map((x) => x.high));
    const tone = (v) => shade(hi > lo ? (v - lo) / (hi - lo) : 1);
    const paintBars = (u) => {
      const { ctx } = u;
      clip(u, ctx);
      const w = Math.max(1, (u.valToPos(86400, "x", true) - u.valToPos(0, "x", true)) * 0.7);
      for (const x of days) {
        const cx = u.valToPos(x.t + 43200, "x", true), top = u.valToPos(x.high, "y", true), bot = u.valToPos(x.low, "y", true);
        const g = ctx.createLinearGradient(0, bot, 0, top);
        g.addColorStop(0, tone(x.low)); g.addColorStop(1, tone(x.high));
        ctx.fillStyle = g;
        ctx.fillRect(cx - w / 2, top, w, Math.max(1, bot - top));
        ctx.fillStyle = css("--fg");
        ctx.fillRect(cx - w / 2, u.valToPos(x.mean, "y", true) - 1, w, 2);
      }
      ctx.restore();
    };
    const hidden = { paths: () => null, points: { show: false } };
    const bandPlot = new uPlot({
      width: band.current.clientWidth, height: 200, cursor: { points: { show: false } }, legend: { live: true },
      scales: { x: { time: true, range: () => span } },
      axes: [axis, { ...yAxis, label }],
      series: [{}, ...["High", "Mean", "Low"].map((l) => ({ ...hidden, label: l, value: (u, v) => (v == null ? "–" : v.toFixed(1)) }))],
      hooks: { draw: [paintBars] },
    }, [days.map((x) => x.t + 43200), days.map((x) => x.high), days.map((x) => x.mean), days.map((x) => x.low)], band.current);

    const cells = days.flatMap((x) => x.hours.filter((v) => v != null));
    const clo = Math.min(...cells), chi = Math.max(...cells);
    const paintCells = (u) => {
      const { ctx } = u;
      clip(u, ctx);
      for (const x of days) {
        const x0 = u.valToPos(x.t, "x", true), x1 = u.valToPos(x.t + 86400, "x", true);
        x.hours.forEach((v, h) => {
          if (v == null) return;
          const y0 = u.valToPos(h, "y", true), y1 = u.valToPos(h + 1, "y", true);
          ctx.fillStyle = shade(chi > clo ? (v - clo) / (chi - clo) : 1);
          ctx.fillRect(x0, Math.min(y0, y1), Math.max(1, x1 - x0), Math.abs(y1 - y0));
        });
      }
      ctx.restore();
    };
    const hover = (u) => {
      const out = readout.current;
      if (!out) return;
      const { left, top } = u.cursor;
      const t = left >= 0 ? u.posToVal(left, "x") : null, h = top >= 0 ? Math.floor(u.posToVal(top, "y")) : null;
      const x = t == null ? null : days.find((x) => t >= x.t && t < x.t + 86400);
      const v = x && h >= 0 && h < 24 ? x.hours[h] : null;
      out.textContent = v == null ? "" : `${x.date} ${String(h).padStart(2, "0")}:00 · ${v.toFixed(1)} ${label}`;
    };
    const heatPlot = new uPlot({
      width: heat.current.clientWidth, height: 240, legend: { show: false },
      cursor: { points: { show: false }, drag: { x: false, y: false } },
      scales: { x: { time: true, range: () => span }, y: { range: [0, 24], dir: -1 } },
      axes: [axis, { ...yAxis, label: "hour", splits: () => [0, 6, 12, 18, 24], values: (u, ticks) => ticks.map((h) => `${h}:00`) }],
      series: [{}, { points: { show: false } }],
      hooks: { draw: [paintCells], setCursor: [hover] },
    }, [span, [null, null]], heat.current);

    // The average day: each hour's mean over the range, with the 10th–90th percentile band around it.
    const pct = (vals, q) => vals.length ? vals[Math.min(vals.length - 1, Math.floor(q * vals.length))] : null;
    const byHour = Array.from({ length: 24 }, (_, h) => days.map((x) => x.hours[h]).filter((v) => v != null).sort((a, b) => a - b));
    const mean = byHour.map((v) => (v.length ? v.reduce((a, b) => a + b, 0) / v.length : null));
    const c1 = css("--chart-1");
    const avgPlot = new uPlot({
      width: avg.current.clientWidth, height: 200, cursor: { points: { size: 6 } }, legend: { live: true },
      scales: { x: { time: false, range: [0, 23] } },
      axes: [{ ...axis, splits: () => [0, 3, 6, 9, 12, 15, 18, 21], values: (u, ticks) => ticks.map((h) => `${h}:00`) }, { ...yAxis, label }],
      series: [{ label: "Hour", value: (u, h) => (h == null ? "–" : `${h}:00`) },
        { label: "90th percentile", stroke: c1 + "66", width: 1, points: { show: false }, value: (u, v) => (v == null ? "–" : v.toFixed(1)) },
        { label: "Mean", stroke: c1, width: 2, points: { show: false }, value: (u, v) => (v == null ? "–" : v.toFixed(1)) },
        { label: "10th percentile", stroke: c1 + "66", width: 1, points: { show: false }, value: (u, v) => (v == null ? "–" : v.toFixed(1)) }],
      bands: [{ series: [1, 3], fill: c1 + "22" }],
    }, [byHour.map((_, h) => h), byHour.map((v) => pct(v, 0.9)), mean, byHour.map((v) => pct(v, 0.1))], avg.current);

    const ro = new ResizeObserver(() => {
      bandPlot.setSize({ width: band.current.clientWidth, height: 200 });
      heatPlot.setSize({ width: heat.current.clientWidth, height: 240 });
      avgPlot.setSize({ width: avg.current.clientWidth, height: 200 });
    });
    ro.observe(band.current); ro.observe(heat.current); ro.observe(avg.current);
    return () => { ro.disconnect(); bandPlot.destroy(); heatPlot.destroy(); avgPlot.destroy(); };
  }, [d, unit, theme]);
  const suffix = name ? ` · ${name}` : "";
  const temps = d ? d.days.some((x) => x.low != null) : false;
  const body = (ref) => !d ? html`<div class="boot">Loading…</div>`
    : !temps ? html`<p class="muted">No temperatures in this range.</p>` : html`<div class="chart" ref=${ref}></div>`;
  return html`
    <div class="card"><h2>Daily high & low${suffix}</h2>${body(band)}</div>
    <div class="card"><h2>Temperature by hour${suffix}</h2>${body(heat)}
      ${temps && html`<p class="muted heatkey"><span>colder</span><i style="background: linear-gradient(90deg, ${shade(0)}, ${shade(1)})"></i>
        <span>warmer · hours in ${d.timezone}</span> <span ref=${readout}></span></p>`}</div>
    <div class="card"><h2>Average day${suffix}</h2>${body(avg)}
      ${temps && html`<p class="muted">Each hour's mean over the range, with the band holding 80% of the days. Hours in ${d.timezone}.</p>`}</div>
    <div class="card"><h2>Calendar${suffix}</h2>${!d ? html`<div class="boot">Loading…</div>`
      : !d.days.length ? html`<p class="muted">Nothing stored in this range.</p>` : html`<${Calendar} days=${d.days} unit=${unit} />`}</div>`;
}

// Parts of the station's local day, for the rose: the breeze often turns between day and night.
const PARTS = [["", "All day"], ["night", "Night 0–6"], ["morning", "Morning 6–12"], ["afternoon", "Afternoon 12–18"], ["evening", "Evening 18–24"]];
function Rose({ device, start, end, unit }) {
  const [part, setPart] = useState("");
  const [r, setR] = useState(null);
  useEffect(() => {
    api(`/api/wx/rose?device=${device}&start=${start}&end=${end}${part ? "&part=" + part : ""}`).then(setR).catch(() => setR(null));
  }, [device, start, end, part]);
  const parts = html`<div class="seg">${PARTS.map(([k, l]) => html`<button key=${k} class=${k === part ? "primary" : ""} onClick=${() => setPart(k)}>${l}</button>`)}</div>`;
  if (!r || !r.total) return html`${parts}<p class="muted">No wind data for this range${part ? " at this time of day" : ""}.</p>`;
  const max = Math.max(...r.counts.map((c) => c.reduce((a, b) => a + b, 0)));
  const tone = (j) => shade(r.bands.length > 1 ? j / (r.bands.length - 1) : 1);   // the Settings → Colors gradient, calm to strong
  const R = 44, step = 22.5, gap = 1.5;
  const arc = (i, r0, r1) => {
    const a0 = ((i * step - step / 2 + gap) - 90) * Math.PI / 180, a1 = ((i * step + step / 2 - gap) - 90) * Math.PI / 180;
    const p = (rad, a) => `${(rad * Math.cos(a)).toFixed(2)},${(rad * Math.sin(a)).toFixed(2)}`;
    return `M${p(r0, a0)} L${p(r1, a0)} A${r1},${r1} 0 0 1 ${p(r1, a1)} L${p(r0, a1)} A${r0},${r0} 0 0 0 ${p(r0, a0)} Z`;
  };
  const bandLabel = (j) => `${fmt(unitsFor(unit).speed[1](r.bands[j]), 0)}${j + 1 < r.bands.length ? "–" + fmt(unitsFor(unit).speed[1](r.bands[j + 1]), 0) : "+"} ${unitsFor(unit).speed[0]}`;
  return html`${parts}<div class="rose"><svg viewBox="-56 -56 112 112" role="img" aria-label="Wind rose">
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
    values: (opts.calc ? opts.calc(data.devices[id]) : data.devices[id][opts.from || field]).map((v) => (v == null ? null : kind ? unitsFor(u)[kind][1](v) : v)),
  }] : []);
  const chart = (title, series, unit, { note, ...extra } = {}) => html`<div class="card"><h2>${title}</h2>
    <${TimeChart} t=${data.t} series=${series} unit=${unit} ...${extra} />${note && html`<p class="muted">${note}</p>`}</div>`;
  // A running total: missing points add nothing.
  const running = (series) => series.map((s) => { let sum = 0; return { ...s, values: s.values.map((v) => (sum += v || 0)) }; });
  const rainDays = (id) => new Set(data.t.filter((t, i) => data.devices[id].rain[i] > 0).map((t) => new Date(t * 1000).toLocaleDateString("sv"))).size;
  const rainDigits = u === "imperial" ? 2 : 1;
  // Pressure now minus three hours ago (the forecaster's tendency), once points are an hour or finer.
  const index = new Map((data?.t || []).map((t, i) => [t, i]));
  const tendency = (dev) => dev.pressure.map((p, i) => { const j = index.get(data.t[i] - 10800), q = j == null ? null : dev.pressure[j];
    return p == null || q == null ? null : p - q; });
  const anyOf = (field) => data.t.map((_, i) => shown.some((id) => data.devices[id]?.[field][i] > 0));
  const swatch = (color) => html`<i class="swatch" style=${`background: var(${color})`}></i>`;
  // Cloud cover from sunlight: how far short of a cloudless sky the station's solar reading falls. Skipped when the
  // sun is low (under 100 W/m² clear-sky), where the ratio is mostly noise.
  const cover = (dev) => dev.solar.map((v, i) => { const c = dev.clear_sky[i];
    return v == null || c == null || c < 100 ? null : 100 * Math.max(0, 1 - v / c); });
  // With both stations shown: the first minus the second, at each point both have data.
  const [a, b] = shown;
  const paired = shown.length === 2 && data?.devices[a] && data?.devices[b];
  const diff = (field, kind, digits) => [{ label: `${names[a]} − ${names[b]}`, color: "--chart-1", digits,
    values: data.devices[a][field].map((v, i) => { const w = data.devices[b][field][i], f = unitsFor(u)[kind][1];
      return v == null || w == null ? null : f(v) - f(w); }) }];
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
      ${chart("Temperature", [...make("temp", "temp"), ...make("dew_point", "temp", { suffix: " dew point", dash: [4, 4], thin: true })], unitsFor(u).temp[0])}
      ${chart("Feels like", [...make("feels_like", "temp"), ...make("temp", "temp", { suffix: " air", dash: [4, 4], thin: true })], unitsFor(u).temp[0], {
        note: "Heat index above 27 °C (80 °F) with humidity over 40%; wind chill at 10 °C (50 °F) or below with some wind; otherwise the air temperature." })}
      ${shown.map((id) => html`<${Days} key=${id} device=${id} name=${shown.length > 1 ? names[id] : ""} start=${data.start} end=${data.end} unit=${u} />`)}
      ${chart("Humidity", make("humidity", null, { digits: 0 }), "%")}
      ${chart("Cloud base (estimated)", make("cloud_base", "height", { digits: 0 }), unitsFor(u).height[0], {
        note: "The lowest per point, above the station, from the temperature–dew point spread. That's where cumulus would form; a station can't see layered overcast, so the real ceiling can be lower. Near zero means fog or saturated air." })}
      ${chart("Pressure (sea level)", make("pressure", "pressure", { digits: u === "imperial" ? 2 : 1 }), unitsFor(u).pressure[0])}
      ${data.bucket <= 3600 && chart("Pressure tendency (change over 3 h)", make("pressure", "pressure", { calc: tendency, digits: u === "imperial" ? 3 : 1 }),
        unitsFor(u).pressure[0], { marks: [{ values: anyOf("rain"), color: "--chart-3" }, { values: anyOf("strikes"), color: "--chart-2" }], step: data.bucket,
          note: html`${swatch("--chart-3")} rain ${swatch("--chart-2")} lightning. A fall of more than ${u === "imperial" ? "0.06 inHg" : "2 hPa"} in 3 hours often comes ahead of a storm.` })}
      ${paired && chart(`Temperature difference · ${names[a]} − ${names[b]}`, diff("temp", "temp", 1), unitsFor(u).temp[0])}
      ${paired && chart(`Pressure difference · ${names[a]} − ${names[b]}`, diff("pressure", "pressure", u === "imperial" ? 3 : 2), unitsFor(u).pressure[0], {
        note: "Sea-level pressure should match closely between nearby stations, so a steady offset here is one barometer reading off." })}
      ${chart("Wind", [...make("wind_avg", "speed", { suffix: " avg" }), ...make("wind_gust", "speed", { suffix: " gust", dash: [4, 4], thin: true })], unitsFor(u).speed[0])}
      ${chart("Gustiness", make("gust_factor", null, { digits: 2 }), "gust ÷ avg", {
        note: "Each reading's gust over its average wind, leaving out near-calm readings. Around 1.3 is steady; 2 and up is turbulent." })}
      ${chart("Rain", make("rain", "rain", { digits: rainDigits }), `${unitsFor(u).rain[0]} per point`, { bars: true })}
      ${chart("Rain, running total", running(make("rain", "rain", { digits: rainDigits })), unitsFor(u).rain[0], {
        note: shown.filter((id) => data.devices[id]).map((id) => { const d = rainDays(id);
          return `${shown.length > 1 ? names[id] + ": " : ""}${d} day${d === 1 ? "" : "s"} with rain`; }).join(" · ") })}
      ${shown.map((id) => html`<div class="card" key=${id}><h2>Lightning${shown.length > 1 ? ` · ${names[id]}` : ""}</h2>
        <${Lightning} device=${id} start=${data.start} end=${data.end} unit=${u} /></div>`)}
      ${chart("Solar radiation", [...make("solar", null, { digits: 0 }), ...make("clear_sky", null, { suffix: " clear sky", dash: [4, 4], thin: true, digits: 0 })], "W/m²")}
      ${chart("Cloud cover (estimated)", make("solar", null, { calc: cover, digits: 0 }), "%", {
        note: "How far the sunlight falls short of a cloudless sky at the station. Daylight only; haze and a dirty sensor read as cloud too." })}
      ${chart("Battery", make("battery", null, { digits: 2 }), "V", {
        note: "The Tempest charges from its solar panel, so the voltage should climb on sunny days. Under about 2.4 V it starts reporting less often to save power." })}
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
