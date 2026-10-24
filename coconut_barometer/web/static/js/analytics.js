// Analytics, in tabs (#/photos/analytics/<tab>): Overview, Photos, Tags, Places, Gear, Audience,
// Albums & groups and "Did it help?". Each tab loads its own data when opened. This site's own visitors are a page of
// their own (#/visitors, analytics_visitors.js).
import { html, useState, useEffect, useMemo, api, num, ago, plain } from "./lib.js";
import { Chart } from "./chart.js";
import { Kpi, TopGrid, usePhotoBox, Range } from "./viz.js";
import { TagsTab } from "./analytics_tags.js";
import { PlacesTab, GearTab, AudienceTab, GroupsTab, ImpactTab } from "./analytics_more.js";

const TABS = [["overview", "Overview"], ["photos", "Photos"], ["tags", "Tags"], ["places", "Places"], ["gear", "Gear & technique"],
              ["audience", "Audience"], ["groups", "Albums & groups"], ["impact", "Did it help?"]];

function Overview({ days, setDays }) {
  const [d, setD] = useState(null);
  const [box, open] = usePhotoBox();
  useEffect(() => { setD(null); api("/api/analytics/overview", { params: { days } }).then(setD); }, [days]);
  const series = d?.series || [];
  const dayList = useMemo(() => series.map((r) => r.day), [d]);
  // Today's views so far: a dashed line on from yesterday, not part of the solid one.
  const views = useMemo(() => {
    const at = series.findIndex((r) => r.partial);
    const out = [{ label: "views", values: series.map((r) => (r.partial ? null : r.views)), color: "--chart-1", gradient: true }];
    if (at >= 0 && series[at].views != null)
      out.push({ label: "so far today", values: series.map((r, i) => (i === at || i === at - 1 ? r.views : null)),
                 color: "--chart-1", dash: [5, 4], fill: false });
    return out;
  }, [d]);
  const social = useMemo(() => [
    { label: "faves", values: series.map((r) => r.faves), color: "--chart-2", bars: true },
    { label: "comments", values: series.map((r) => r.comments), color: "--chart-3", bars: true }], [d]);
  if (!d) return html`<div class="toolbar"><${Range} days=${days} setDays=${setDays} /></div><div class="boot">Loading…</div>`;
  const prev = d.previous;
  const h = d.highlights;
  return html`<div class="toolbar"><${Range} days=${days} setDays=${setDays} /></div>
    <div class="kpis">
      <${Kpi} label="Views" now=${d.period.views} before=${prev?.views} note=${days >= 3650 ? "in the days collected" : `in ${days} days`} />
      <${Kpi} label="Faves received" now=${d.period.faves} before=${prev?.faves} />
      <${Kpi} label="Comments received" now=${d.period.comments} before=${prev?.comments} />
      <${Kpi} label="All-time views" now=${d.totals.views} note=${`on ${num(d.totals.photos)} photos`} />
    </div>
    <div class="cols">
      <div class="card"><h2>Views per day</h2><${Chart} days=${dayList} series=${views} /></div>
      <div class="card"><h2>Faves and comments per day</h2><${Chart} days=${dayList} series=${social} /></div>
    </div>
    <h2 class="section">This week's highlights</h2>
    <div class="cols3">
      <div class="card"><h2>Gaining fastest</h2>
        <${TopGrid} rows=${h.gaining} caption=${(r) => `+${num(r.gain)} views`} onOpen=${open} empty="Not enough days collected yet." /></div>
      <div class="card"><h2>New faves</h2>${h.faves.map((f, i) => html`<div class="feed" key=${i} onClick=${() => open(h.faves, i)}>
          <img alt="" loading="lazy" src=${f.sizes.q || ""} /><span><b>${f.who}</b> faved <i>${f.title || "Untitled"}</i>
          <span class="muted"> · ${ago(f.at)}</span></span></div>`)}</div>
      <div class="card"><h2>New comments</h2>${h.comments.map((c, i) => html`<div class="feed" key=${i} onClick=${() => open(h.comments, i)}>
          <img alt="" loading="lazy" src=${c.sizes.q || ""} /><span><b>${c.who}</b> on <i>${c.title || "Untitled"}</i>
          <span class="muted"> · ${ago(c.at)}</span><br /><span class="quote">${plain(c.text).slice(0, 140)}</span></span></div>`)}</div>
    </div>
    <p class="muted small">Per-day numbers cover the ${num(d.days_collected)} days collected so far; they grow by one each day.
      Flickr's days run midnight to midnight GMT: today's views so far are dashed, and not in the totals until the day is over.</p>
    ${box}`;
}

const LISTS = [
  ["views", "Most viewed", "Views in the period (all-time totals for All)."],
  ["faves", "Most faved", "Faves in the period."],
  ["comments", "Most commented", "Comments in the period."],
  ["gaining", "Gaining", "Views in the last period against the period before it (up to 90 days)."],
  ["sleepers", "Sleepers", "Photos over a year old getting far more views lately than their lifetime average (last 30 days)."],
  ["engagement", "Best engagement", "Faves per 1,000 views, all time, among photos with 100+ views."],
  ["overlooked", "Overlooked", "Well liked (top quarter for faves per view) but seen less than most, and in no group: worth sharing to groups."],
];

function Photos({ days, setDays }) {
  const [kind, setKind] = useState("views");
  const [rows, setRows] = useState(null);
  const [box, open] = usePhotoBox();
  useEffect(() => { setRows(null); api("/api/analytics/photos", { params: { kind, days, limit: 36 } }).then(setRows); }, [kind, days]);
  const about = LISTS.find((l) => l[0] === kind);
  const usesRange = ["views", "faves", "comments", "gaining"].includes(kind);
  const caption = (r) => kind === "sleepers" ? `${r.value}× usual · ${num(r.recent)} views`
    : ["engagement", "overlooked"].includes(kind) ? `${r.value} faves / 1k views · ${num(r.views)} views`
    : kind === "gaining" ? `+${num(r.value)} views` : `${num(r.value)} ${kind}`;
  return html`<div class="toolbar">
      <span class="seg">${LISTS.map(([k, label]) => html`<button class=${kind === k ? "primary" : ""} onClick=${() => setKind(k)}>${label}</button>`)}</span>
      ${usesRange && html`<${Range} days=${days} setDays=${setDays} />`}
    </div>
    <p class="muted">${about[2]}</p>
    ${!rows ? html`<div class="boot">Loading…</div>` : html`<div class="card"><${TopGrid} rows=${rows} caption=${caption} onOpen=${open} /></div>`}
    ${box}`;
}

export function Analytics({ tab = "overview" }) {
  const [days, setDays] = useState(30);
  const current = TABS.some(([t]) => t === tab) ? tab : "overview";
  const page = {
    overview: html`<${Overview} days=${days} setDays=${setDays} />`, photos: html`<${Photos} days=${days} setDays=${setDays} />`,
    tags: html`<${TagsTab} />`, places: html`<${PlacesTab} />`, gear: html`<${GearTab} />`, audience: html`<${AudienceTab} />`,
    groups: html`<${GroupsTab} />`, impact: html`<${ImpactTab} />`,
  }[current];
  return html`<div class="analytics">
    <nav class="subtabs" aria-label="Analytics">${TABS.map(([t, label]) => html`<a href=${"#/photos/analytics/" + t}
        class=${t === current ? "on" : ""}>${label}</a>`)}</nav>
    ${page}
  </div>`;
}

