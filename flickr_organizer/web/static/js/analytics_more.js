// Analytics tabs: Places, Gear & technique, Audience, Albums & groups, Did it help?
import { html, useState, useEffect, useMemo, api, num, day, go } from "./lib.js";
import { Chart } from "./chart.js";
import { Bars, Table, Heatmap, pct, times } from "./viz.js";

const loading = html`<div class="boot">Loading…</div>`;

function useData(path, params = {}) {
  const [d, setD] = useState(null);
  const key = JSON.stringify(params);
  useEffect(() => { setD(null); api(path, { params }).then(setD).catch((e) => setD({ error: e.message })); }, [path, key]);
  return d;
}

// ---- places -------------------------------------------------------------------------------
const ZOOM = { country: 5, state: 7, city: 12 };

export function PlacesTab() {
  const [level, setLevel] = useState("country");
  const rows = useData("/api/analytics/places", { level });
  const best = useMemo(() => (rows && !rows.error ? [...rows].filter((r) => r.photos >= 10).sort((a, b) => b.avg_views - a.avg_views).slice(0, 15) : []), [rows]);
  const toMap = (r) => go(`map/@${r.lat},${r.lon},${ZOOM[level]}`);
  return html`<div class="toolbar"><span class="seg">${[["country", "Countries"], ["state", "States & provinces"], ["city", "Towns"]].map(([k, label]) =>
      html`<button class=${level === k ? "primary" : ""} onClick=${() => setLevel(k)}>${label}</button>`)}</span>
      <span class="muted">Click one to see it on the map.</span></div>
    ${!rows ? loading : html`<div class="cols">
      <div class="card"><h2>Most views</h2>
        <${Bars} rows=${rows.slice(0, 20)} value="views" onPick=${toMap} right=${(r) => num(r.views)}
          title=${(r) => `${num(r.photos)} photos · ${num(r.faves)} faves · ${num(r.avg_views)} views per photo`} /></div>
      <div class="card"><h2>Best per photo <span class="muted">(10+ photos)</span></h2>
        <p class="muted small">Where your photos draw the most views each, not just where you took the most.</p>
        <${Bars} rows=${best} value="avg_views" onPick=${toMap} right=${(r) => `${num(r.avg_views)} each`}
          title=${(r) => `${num(r.photos)} photos · ${num(r.views)} views`} /></div>
    </div>
    <div class="card"><h2>All</h2><${Table} rows=${rows} onPick=${toMap} cols=${[{ key: "label", label: "Place" },
      { key: "photos", label: "Photos", num: true }, { key: "views", label: "Views", num: true },
      { key: "avg_views", label: "Per photo", num: true }, { key: "faves", label: "Faves", num: true },
      { key: "comments", label: "Comments", num: true }]} /></div>`}`;
}

// ---- gear & technique ------------------------------------------------------------------------
const MEASURES = [["photos", "Photos taken"], ["avg_views", "Views per photo"], ["lift", "Lift"]];
const DIMS = [["camera", "Cameras"], ["lens", "Lenses"], ["focal", "Focal length (35 mm equivalent)"], ["aperture", "Aperture"],
              ["iso", "ISO"], ["light", "Light (sun height where it was taken)"], ["hour", "Hour taken"], ["weekday", "Day taken"]];

export function GearTab() {
  const d = useData("/api/analytics/gear");
  const [measure, setMeasure] = useState("photos");
  const right = (r) => measure === "lift" ? times(r.lift) : measure === "avg_views" ? `${num(r.avg_views)} each` : num(r.photos);
  return html`<div class="toolbar"><span class="muted">Bars show:</span>
      <span class="seg">${MEASURES.map(([k, label]) => html`<button class=${measure === k ? "primary" : ""} onClick=${() => setMeasure(k)}>${label}</button>`)}</span></div>
    <p class="muted">Lift is the median views of those photos against typical photos uploaded the same year (1× = typical), so it isn't skewed by a few viral photos or by older photos having had longer to collect views.</p>
    ${!d ? loading : html`<div class="cols">${DIMS.map(([k, label]) => d[k]?.length > 0 && html`<div class="card" key=${k}><h2>${label}</h2>
        ${k === "light" && html`<p class="muted small">From the geotag and the camera's clock; the time zone is guessed from the longitude, so it can be an hour out.</p>`}
        <${Bars} rows=${d[k]} value=${measure} right=${right}
          title=${(r) => `${num(r.photos)} photos · ${num(r.avg_views)} views each · lift ${times(r.lift)}`} /></div>`)}</div>`}`;
}

// ---- audience ------------------------------------------------------------------------------
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function AudienceTab() {
  const d = useData("/api/analytics/audience");
  const cells = useMemo(() => {
    if (!d || d.error) return null;
    const shift = Math.round(-new Date().getTimezoneOffset() / 60);     // UTC → your time zone
    const local = Array(168).fill(0);
    d.week_utc.forEach((v, i) => { local[(i + shift + 168) % 168] += v; });
    return DAYS.map((_, y) => local.slice(y * 24, y * 24 + 24));
  }, [d]);
  const months = useMemo(() => (d?.monthly || []).map((m) => m.month + "-01"), [d]);
  const people = useMemo(() => [
    { label: "new favers", values: (d?.monthly || []).map((m) => m.new), color: "--chart-2", bars: true },
    { label: "returning", values: (d?.monthly || []).map((m) => m.returning), color: "--chart-1", bars: true }], [d]);
  if (!d) return loading;
  return html`<div class="cols">
      <div class="card"><h2>When your audience faves <span class="muted">(last year, your time)</span></h2>
        <${Heatmap} cells=${cells} rows=${DAYS} cols=${[...Array(24).keys()].map((h) => String(h).padStart(2, "0"))} />
        <p class="muted small">Darker: more faves in that hour. Uploading just before the busiest hours gives a photo the best first day.</p></div>
      <div class="card"><h2>How soon faves arrive</h2>
        <${Bars} rows=${d.timing} value="faves" right=${(r) => num(r.faves)} />
        <p class="muted small">Time from upload to fave, over every fave recorded.</p></div>
    </div>
    <div class="card"><h2>New and returning favers per month <span class="muted">(${num(d.people)} people in all)</span></h2>
      <${Chart} days=${months} series=${people} height=${200} /></div>
    <div class="cols">
      <div class="card"><h2>Top favers</h2><${Table} rows=${d.favers} cols=${[{ key: "name", label: "Who" },
        { key: "faves", label: "Faves", num: true }, { key: "first", label: "Since", num: true, fmt: (v) => day(v) },
        { key: "last", label: "Latest", num: true, fmt: (v) => day(v) }]} /></div>
      <div class="card"><h2>Top commenters</h2><${Table} rows=${d.commenters} cols=${[{ key: "name", label: "Who" },
        { key: "comments", label: "Comments", num: true }, { key: "last", label: "Latest", num: true, fmt: (v) => day(v) }]} /></div>
    </div>`;
}

// ---- albums & groups ------------------------------------------------------------------------
export function GroupsTab() {
  const d = useData("/api/analytics/groups");
  if (!d) return loading;
  const g = d.group_lift;
  const side = (label, s) => html`<div class="kpi"><span>${label}</span><b>${times(s.lift)}</b>
    <small> ${num(s.photos)} photos · ${num(s.avg_views)} views each · ${s.faves_per_1k} faves/1k</small></div>`;
  return html`<h2 class="section">Do groups help?</h2>
    <div class="kpis">${side("Photos in groups", g.in_groups)}${side("Photos in no group", g.not_in_groups)}</div>
    <p class="muted small">Lift against typical photos uploaded the same year. Photos you put in groups may be your better ones to begin with, so read it as a hint, not proof.</p>
    <div class="cols">
      <div class="card"><h2>Groups <span class="muted">(3+ of your photos)</span></h2><${Table} rows=${d.groups} initial="lift" cols=${[
        { key: "name", label: "Group", fmt: (v, r) => html`<a href=${`https://www.flickr.com/groups/${encodeURIComponent(r.path_alias || r.id)}/`}
            target="_blank" rel="noopener noreferrer">${v}</a>` },
        { key: "members", label: "Members", num: true }, { key: "photos", label: "Yours", num: true },
        { key: "avg_views", label: "Per photo", num: true }, { key: "lift", label: "Lift", num: true, fmt: times }]} /></div>
      <div class="card"><h2>Albums</h2><${Table} rows=${d.albums} onPick=${(r) => go("albums/" + r.id)} cols=${[
        { key: "title", label: "Album" }, { key: "photos", label: "Photos", num: true }, { key: "views", label: "Views", num: true },
        { key: "avg_views", label: "Per photo", num: true }, { key: "faves_per_1k", label: "Faves/1k", num: true, fmt: (v) => v },
        { key: "lift", label: "Lift", num: true, fmt: times }]} /></div>
    </div>`;
}

// ---- did it help? ------------------------------------------------------------------------------
export function ImpactTab() {
  const d = useData("/api/analytics/impact", { window: 14 });
  if (!d) return loading;
  const waiting = d.kinds.every((k) => !k.counted);
  return html`<p class="muted">For each kind of change this app made, the average daily views of those photos in the
      ${d.window} days before against the days after, next to photos it didn't change that way over the same days.
      The difference is the effect: it allows for busy and quiet weeks.</p>
    ${waiting && html`<p class="problem">Not enough days yet: the changes need daily stats from before and after them.
      Per-day stats run ${d.first_day || "–"} to ${d.last_day || "–"} (${num(d.days_collected)} days); this fills in over the next ${d.window} days.</p>`}
    <div class="cols">${d.kinds.map((k) => html`<div class="card impact" key=${k.kind}><h2>${k.kind}</h2>
      <p class="muted small">${num(k.photos)} photos changed${k.counted ? ` · ${num(k.counted)} with days on both sides · ${k.after_days} day${k.after_days === 1 ? "" : "s"} after so far` : ""}</p>
      ${k.counted ? html`<div class="kpis small">
          <div class="kpi"><span>Views a day, before</span><b>${k.before.toFixed(2)}</b></div>
          <div class="kpi"><span>After</span><b>${k.after.toFixed(2)}</b><small class=${k.change >= 0 ? "up" : "down"}> ${pct(k.change)}</small></div>
          <div class="kpi"><span>Unchanged photos</span><b>${pct(k.control_change)}</b><small> ${num(k.control)} photos</small></div>
          <div class="kpi"><span>Effect</span><b class=${k.effect > 0 ? "up" : k.effect < 0 ? "down" : ""}>${k.effect == null ? "–" : pct(k.effect)}</b>
            ${k.effect == null && html`<small> too few unchanged photos to compare</small>`}</div>
        </div>` : html`<p class="muted">Waiting for days after the change.</p>`}
    </div>`)}</div>`;
}
