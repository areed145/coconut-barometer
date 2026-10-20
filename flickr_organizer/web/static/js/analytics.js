// Views, faves and comments over time; top photos, gainers and breakdowns.
import { html, useState, useEffect, useMemo, api, num, pick } from "./lib.js";
import { Chart } from "./chart.js";
import { Lightbox } from "./gallery.js";

const RANGES = [[7, "7 days"], [30, "30 days"], [90, "90 days"], [365, "1 year"], [3650, "All"]];
const asPhoto = (r) => ({ id: r.photo_id, title: r.title, sizes: r.sizes || {}, views: r.views, faves: r.favorites, comments: r.comments });

function TopGrid({ rows, metric, onOpen }) {
  if (!rows?.length) return html`<p class="muted">No stats in this range yet.</p>`;
  return html`<div class="toplist">${rows.map((r, i) => html`<button class="tile" key=${r.photo_id} onClick=${() => onOpen(rows, i)}
      aria-label=${r.title || "Untitled photo"}>
    <img loading="lazy" alt="" src=${pick(r.sizes, ["n", "w", "q"])} />
    <span class="cap">${num(r[metric])} ${metric === "favorites" ? "faves" : "views"}</span></button>`)}</div>`;
}

function Bars({ rows }) {
  if (!rows?.length) return html`<p class="muted">No stats in this range yet.</p>`;
  const max = Math.max(...rows.map((r) => r.views || 0), 1);
  return html`<div class="bars">${rows.map((r) => html`<div class="bar" title=${`${num(r.views)} views · ${num(r.faves)} faves · ${num(r.photos)} photos`}>
    <span class="label">${r.label}</span>
    <span class="track"><span class="fill" style=${`width:${(100 * (r.views || 0)) / max}%`}></span></span>
    <span class="n">${num(r.views)}</span></div>`)}</div>`;
}

export function Analytics() {
  const [days, setDays] = useState(30);
  const [by, setBy] = useState("album");
  const [data, setData] = useState(null);
  const [gainers, setGainers] = useState(null);
  const [breakdown, setBreakdown] = useState(null);
  const [open, setOpen] = useState(null);   // {photos, index}
  useEffect(() => {
    setData(null);
    api("/api/analytics/overview", { params: { days } }).then(setData);
    api("/api/analytics/gainers", { params: { days: Math.min(days, 90), limit: 12 } }).then(setGainers);
  }, [days]);
  useEffect(() => { api("/api/analytics/breakdown", { params: { by, days, limit: 15 } }).then(setBreakdown); }, [by, days]);

  const series = data?.series || [];
  const dayList = useMemo(() => series.map((r) => r.day), [data]);
  const viewSeries = useMemo(() => [{ label: "views", values: series.map((r) => r.views), color: "--chart-1" }], [data]);
  const socialSeries = useMemo(() => [
    { label: "faves", values: series.map((r) => r.faves), color: "--chart-2", bars: true },
    { label: "comments", values: series.map((r) => r.comments), color: "--chart-3", bars: true }], [data]);
  const sum = (k) => series.reduce((a, r) => a + (r[k] || 0), 0);
  const openRows = (rows, index) => setOpen({ photos: rows.map(asPhoto), index });

  return html`
    <div class="toolbar">
      <h1 class="title" style="margin:0 12px 0 0">Analytics</h1>
      ${RANGES.map(([d, label]) => html`<button class=${days === d ? "primary" : ""} onClick=${() => setDays(d)}>${label}</button>`)}
    </div>
    ${!data ? html`<div class="boot">Loading…</div>` : html`
      <div class="kpis">
        <div class="kpi"><span>Views</span><b>${num(sum("views"))}</b><small> in ${days >= 3650 ? "all days collected" : `${days} days`}</small></div>
        <div class="kpi"><span>Faves received</span><b>${num(sum("faves"))}</b></div>
        <div class="kpi"><span>Comments received</span><b>${num(sum("comments"))}</b></div>
        <div class="kpi"><span>All-time views</span><b>${num(data.totals.views)}</b><small> on ${num(data.totals.photos)} photos</small></div>
      </div>
      <div class="cols">
        <div class="card"><h2>Views per day</h2><${Chart} days=${dayList} series=${viewSeries} /></div>
        <div class="card"><h2>Faves and comments per day</h2><${Chart} days=${dayList} series=${socialSeries} /></div>
      </div>
      <div class="cols">
        <div class="card"><h2>Most viewed</h2><${TopGrid} rows=${data.top} metric="views" onOpen=${openRows} /></div>
        <div class="card"><h2>Most faved</h2><${TopGrid} rows=${data.top_faved} metric="favorites" onOpen=${openRows} /></div>
      </div>
      <div class="cols">
        <div class="card"><h2>Gaining views <span class="muted">(last ${Math.min(days, 90)} days vs the ${Math.min(days, 90)} before)</span></h2>
          ${!gainers?.length ? html`<p class="muted">Not enough days collected yet.</p>`
            : gainers.map((g, i) => html`<div class="gainer" key=${g.photo_id} onClick=${() => openRows(gainers, i)}>
                <img alt="" loading="lazy" src=${pick(g.sizes, ["q", "n"])} />
                <span class="t">${g.title || "Untitled"}</span>
                <span class="up">+${num(g.gain)}</span></div>`)}
        </div>
        <div class="card"><h2>Views by <select value=${by} onChange=${(e) => setBy(e.target.value)} aria-label="Group by">
            <option value="album">album</option><option value="camera">camera</option>
            <option value="year">year taken</option><option value="media">photo / video</option></select></h2>
          <${Bars} rows=${breakdown} /></div>
      </div>`}
    ${open && html`<${Lightbox} photos=${open.photos} index=${open.index} onIndex=${(i) => setOpen({ ...open, index: i })}
        onClose=${() => setOpen(null)} />`}`;
}
