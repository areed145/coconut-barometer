// The Tags tab: the top tags by any measure as ranked bars, niche winners vs overused tags, trending tags,
// a sortable table, and a tag's own page (its photos, views over time, tags it goes with).
import { html, useState, useEffect, useMemo, api, num, photosLink } from "./lib.js";
import { Chart } from "./chart.js";
import { Bars, Scatter, Table, TopGrid, usePhotoBox, pct, times } from "./viz.js";

const METRICS = [
  ["photos", "Use", "How many of your photos carry the tag", (t) => `${num(t.photos)} photos`],
  ["views", "Total views", "Views of all its photos: what brings traffic overall", (t) => `${num(t.views)} views`],
  ["avg_views", "Views per photo", "Average views of its photos: which tags are worth adding", (t) => `${num(t.avg_views)} views per photo`],
  ["faves_per_1k", "Faves per 1k views", "Faves per 1,000 views: the subjects people love", (t) => `${t.faves_per_1k} faves per 1k views`],
  ["comments", "Comments", "Comments on its photos: the subjects that start conversations", (t) => `${num(t.comments)} comments`],
  ["lift", "Lift", "Median views of its photos against typical photos uploaded the same year (1× = typical)", (t) => `${times(t.lift)} typical views`],
];
const SOURCES = [["all", "All tags"], ["yours", "Yours"], ["added", "Added here"]];
const KINDS = [["date", "dates"], ["common", "on most photos"], ["place", "places"], ["camera", "cameras"]];
const tone = (t) => (t.trend == null ? "" : t.trend > 0.15 ? "up" : t.trend < -0.15 ? "down" : "");

function TagPage({ tag, onPick, onClose }) {
  const [d, setD] = useState(null);
  const [box, open] = usePhotoBox();
  useEffect(() => { setD(null); api(`/api/analytics/tags/${encodeURIComponent(tag)}`).then(setD); }, [tag]);
  const days = useMemo(() => (d?.series || []).map((r) => r.day), [d]);
  const series = useMemo(() => [{ label: "views", values: (d?.series || []).map((r) => r.views), color: "--chart-1" }], [d]);
  return html`<div class="card tagpage"><h2>#${tag} <span><a class="small" href=${photosLink({ tag })}>All its photos →</a>
      <button class="link" onClick=${onClose} aria-label="Close">✕</button></span></h2>
    ${!d ? html`<p class="muted">Loading…</p>` : html`
      <p class="muted">${num(d.photos)} photos · ${num(d.views)} views · ${num(d.faves)} faves</p>
      ${d.often_with.length > 0 && html`<p class="chips"><span class="why">Often with:</span>${d.often_with.map((o) => html`<button
          class="chip" onClick=${() => onPick(o.tag)} title=${`on ${Math.round(o.share * 100)}% of its photos`}>${o.tag}</button>`)}</p>`}
      <${Chart} days=${days} series=${series} height=${160} />
      <h3>Most viewed</h3>
      <${TopGrid} rows=${d.top} caption=${(r) => `${num(r.views)} views`} onOpen=${open} />`}
    ${box}</div>`;
}

export function TagsTab() {
  const [metric, setMetric] = useState("photos");
  const [source, setSource] = useState("all");
  const [hide, setHide] = useState(["date", "common"]);
  const [min, setMin] = useState(3);
  const [data, setData] = useState(null);
  const [tag, setTag] = useState(null);
  useEffect(() => {
    setData(null);
    api("/api/analytics/tags", { params: { sort: metric, source, hide: hide.join(","), min_photos: min, limit: 400 } }).then(setData);
  }, [metric, source, hide.join(","), min]);
  const m = METRICS.find((x) => x[0] === metric);
  const tags = data?.tags || [];
  const ranked = useMemo(() => tags.slice(0, 40).map((t) => ({ ...t, label: t.tag })), [data]);
  const fmt = { photos: (t) => num(t.photos), views: (t) => num(t.views), avg_views: (t) => num(t.avg_views),
                faves_per_1k: (t) => t.faves_per_1k, comments: (t) => num(t.comments), lift: (t) => times(t.lift) }[metric];
  const arrow = (t) => (t.trend == null ? "" : t.trend > 0.15 ? " ▲" : t.trend < -0.15 ? " ▼" : "");
  const points = useMemo(() => tags.map((t) => ({ label: t.tag, x: t.photos, y: t.avg_views, tone: tone(t),
    r: 3 + Math.min(6, Math.sqrt(t.faves_per_1k || 0) / 1.5),
    title: `${t.tag}: ${num(t.photos)} photos, ${num(t.avg_views)} views each, lift ${times(t.lift)}` })), [data]);
  const trending = useMemo(() => tags.filter((t) => t.trend != null).sort((a, b) => b.trend - a.trend).slice(0, 15), [data]);
  const toggle = (k) => setHide(hide.includes(k) ? hide.filter((x) => x !== k) : [...hide, k]);
  const pick = (t) => { setTag(t); setTimeout(() => document.querySelector(".tagpage")?.scrollIntoView({ behavior: "smooth", block: "start" }), 50); };
  return html`
    <div class="toolbar">
      <span class="seg">${METRICS.map(([k, label]) => html`<button class=${metric === k ? "primary" : ""} onClick=${() => setMetric(k)}>${label}</button>`)}</span>
    </div>
    <div class="toolbar">
      <select value=${source} onChange=${(e) => setSource(e.target.value)} aria-label="Which tags">
        ${SOURCES.map(([k, label]) => html`<option value=${k}>${label}</option>`)}</select>
      <span class="group-inline"><span class="muted">Hide:</span>
        ${KINDS.map(([k, label]) => html`<label class="check"><input type="checkbox" checked=${hide.includes(k)} onChange=${() => toggle(k)} /> ${label}</label>`)}</span>
      <label class="check">at least <select value=${min} onChange=${(e) => setMin(+e.target.value)} aria-label="Minimum photos">
        ${[1, 3, 5, 10, 25, 100].map((n) => html`<option value=${n}>${n}</option>`)}</select> photos</label>
      <span class="spacer"></span><span class="muted">${data ? `${num(data.total)} tags` : ""}</span>
    </div>
    ${tag && html`<${TagPage} tag=${tag} onPick=${pick} onClose=${() => setTag(null)} />`}
    ${!data ? html`<div class="boot">Loading…</div>` : html`
      <div class="card"><h2>Top 40 by ${m[1].toLowerCase()} <span class="muted">· ${m[2]}</span></h2>
        <div class="tagbars"><${Bars} rows=${ranked} value=${metric} onPick=${(r) => pick(r.tag)}
          right=${fmt} after=${(t) => html`<span class=${t.trend > 0.15 ? "up" : t.trend < -0.15 ? "down" : ""}>${arrow(t).trim()}</span>`}
          title=${(t) => `${t.tag}: ${m[3](t)} · ${num(t.photos)} photos${t.trend != null ? ` · ${pct(t.trend)} views in 14 days` : ""}`} /></div>
        <p class="muted small">Bar color follows Settings → Photos → Colors. ▲ ▼: views rising or falling over the last 14 days against the 14 before
          (of the ${num(data.days_collected)} days collected). Click a tag for its page.</p>
      </div>
      <div class="cols">
        <div class="card"><h2>Niche winners and overused tags</h2>
          <${Scatter} points=${points} xLabel="photos with the tag" yLabel="average views per photo" onPick=${pick}
            corners=${["niche winners: tag more photos with these", "overused: on many photos, few views each"]} />
          <p class="muted small">Each dot is a tag; size is faves per view. Lines are the medians.</p></div>
        <div class="card"><h2>Trending</h2>
          ${trending.length ? trending.map((t) => html`<div class="trend" key=${t.tag} onClick=${() => pick(t.tag)}>
              <span>${t.tag}</span><span class="muted">${num(t.recent)} views</span><span class=${t.trend >= 0 ? "up" : "down"}>${pct(t.trend)}</span></div>`)
            : html`<p class="muted">Needs a few weeks of daily stats.</p>`}</div>
      </div>
      <div class="card"><h2>All tags</h2>
        <${Table} rows=${tags} initial=${metric} onPick=${(r) => pick(r.tag)} cols=${[
          { key: "tag", label: "Tag" }, { key: "kind", label: "Kind" }, { key: "photos", label: "Photos", num: true },
          { key: "views", label: "Views", num: true }, { key: "avg_views", label: "Per photo", num: true },
          { key: "median_views", label: "Median", num: true }, { key: "faves_per_1k", label: "Faves/1k", num: true, fmt: (v) => v },
          { key: "comments", label: "Comments", num: true }, { key: "lift", label: "Lift", num: true, fmt: times },
          { key: "trend", label: "14 days", num: true, fmt: (v) => html`<span class=${v > 0 ? "up" : v < 0 ? "down" : ""}>${pct(v)}</span>` }]} />
      </div>`}`;
}
