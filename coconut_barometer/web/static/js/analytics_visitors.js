// Visitors (#/visitors, #83): who opens this site, on every page of it (not Flickr). Counted by the page itself
// (main.js → /api/seen), without cookies or IP addresses; see visitors.py on the server.
import { html, useState, useEffect, useMemo, api, num, ago, go } from "./lib.js";
import { Chart } from "./chart.js";
import { Kpi, Bars, Table, TopGrid, usePhotoBox, Range } from "./viz.js";

const PAGES = {
  "/home": "Home", "/photos": "Photos", "/photos/photostream": "Photos", "/photos/albums": "Albums",
  "/photos/map": "Photo map", "/photos/photo/:id": "A photo", "/weather": "Weather: live", "/weather/live": "Weather: live",
  "/weather/history": "Weather: history", "/weather/records": "Weather: records", "/weather/map": "Weather: map",
  "/aprs": "APRS: tracking", "/aprs/tracking": "APRS: tracking", "/aprs/igate": "APRS: igate", "/login": "Sign in",
};
const page = (route) => PAGES[route] || route;
// Routes that read the same (#/photos and #/photos/photostream) as one row.
const byPage = (rows) => Object.values(rows.reduce((out, r) => {
  const label = page(r.label), o = out[label] || (out[label] = { label, views: 0, visitors: 0 });
  o.views += r.views; o.visitors += r.visitors;
  return out;
}, {})).sort((a, b) => b.views - a.views);
const DEVICES = { phone: "Phone", tablet: "Tablet", desktop: "Computer" };
const WHY = { owner: "from your own devices", gpc: "asked not to be tracked", "cross-site": "sent from another site",
              rate: "over the rate limit", limit: "over a visitor's daily limit", malformed: "malformed", buffer: "buffer full" };

// Every day from `since` to today (UTC), so days nobody came show as 0.
function allDays(since) {
  const out = [];
  for (let d = new Date(since + "T00:00:00Z"); d <= new Date(); d.setUTCDate(d.getUTCDate() + 1)) out.push(d.toISOString().slice(0, 10));
  return out;
}

export function Visitors() {
  const [days, setDays] = useState(30);
  const range = html`<${Range} days=${days} setDays=${setDays} />`;
  const [d, setD] = useState(null);
  const [box, open] = usePhotoBox();
  useEffect(() => { setD(null); api("/api/analytics/visitors", { params: { days } }).then(setD).catch((e) => setD({ error: e.message })); }, [days]);
  const dayList = useMemo(() => (d?.series ? allDays(d.series[0]?.day < d.since ? d.series[0].day : d.since) : []), [d]);
  const series = useMemo(() => {
    if (!d?.series) return [];
    const by = Object.fromEntries(d.series.map((r) => [r.day, r]));
    return [{ label: "page views", values: dayList.map((day) => by[day]?.views || 0), color: "--chart-1", gradient: true },
            { label: "visitors", values: dayList.map((day) => by[day]?.visitors || 0), color: "--chart-2", bars: true }];
  }, [d, dayList]);
  if (!d) return html`<div class="toolbar">${range}</div><div class="boot">Loading…</div>`;
  if (d.error) return html`<div class="toolbar">${range}</div><p class="problem">${d.error}</p>`;
  const t = d.totals, prev = d.previous, c = d.collector;
  const bars = (rows, label = (r) => r.label) => html`<${Bars} rows=${rows.map((r) => ({ ...r, label: label(r) }))} value="views"
      right=${(r) => num(r.views)} title=${(r) => `${num(r.visitors)} visitors · ${num(r.views)} page views`} empty="Nobody yet." />`;
  const leftOut = Object.entries(c.left_out || {}).filter(([, n]) => n);
  return html`<div class="toolbar">${range}</div>
    ${!c.enabled && html`<p class="problem">Visitors aren't being counted: turn it on in <a href="#/settings/visitors">Settings → Visitors</a>.</p>`}
    <div class="kpis">
      <${Kpi} label="Visitors" now=${t.visitors} before=${prev?.visitors} note=${days > d.keep_days ? `in the ${d.keep_days} days kept` : `in ${days} days`} />
      <${Kpi} label="Page views" now=${t.views} before=${prev?.views} />
      <${Kpi} label="Visits" now=${t.sessions} before=${prev?.sessions} note="(a new one after 30 min idle)" />
      <${Kpi} label="Here now" now=${t.now} note="in the last 5 minutes" />
    </div>
    <div class="card"><h2>Visitors and page views per day</h2><${Chart} days=${dayList} series=${series} /></div>
    <div class="cols">
      <div class="card"><h2>Pages</h2>${bars(byPage(d.pages))}</div>
      <div class="card"><h2>Where they came from</h2>${bars(d.referrers)}
        ${d.campaigns.length > 0 && html`<h3>Campaigns <span class="muted">(utm_ in the link)</span></h3>${bars(d.campaigns)}`}</div>
    </div>
    <div class="cols">
      <div class="card"><h2>Cities</h2>${bars(d.cities)}
        <p class="muted small">From each visitor's IP address. Phones on a carrier's network often show the carrier's nearest hub city.</p></div>
      <div class="card"><h2>Countries</h2>${bars(d.countries)}<h3>States & regions</h3>${bars(d.regions)}</div>
    </div>
    <div class="cols3">
      <div class="card"><h2>Devices</h2>${bars(d.devices, (r) => DEVICES[r.label] || r.label)}</div>
      <div class="card"><h2>Browsers</h2>${bars(d.browsers)}</div>
      <div class="card"><h2>Systems</h2>${bars(d.systems)}</div>
    </div>
    <div class="card"><h2>Photos opened here</h2>
      <${TopGrid} rows=${d.photos} onOpen=${open} empty="No photos opened yet."
        caption=${(r) => `${num(r.views)} here · ${num(r.flickr_views)} on Flickr`} /></div>
    <div class="cols">
      <div class="card"><h2>Shared links previewed</h2>
        <p class="muted small">A /p/ or /a/ link pasted into a chat or post, fetched by that app to show its preview.</p>
        ${d.previews.length ? html`<${Table} rows=${d.previews} onPick=${(r) => go(r.route === "/p/:id" ? `photos/photo/${r.item}` : `photos/photostream?album=${r.item}`)}
          cols=${[{ key: "title", label: "Photo or album", fmt: (v) => v || "Untitled" }, { key: "apps", label: "Where" },
                  { key: "previews", label: "Previews", num: true }]} />` : html`<p class="muted">None yet.</p>`}</div>
      <div class="card"><h2>Bots left out</h2>
        ${d.bots.length ? html`<${Bars} rows=${d.bots} value="views" right=${(r) => num(r.views)} />` : html`<p class="muted">None.</p>`}
        ${leftOut.length > 0 && html`<p class="muted small">${`Also not counted since the app started: ${leftOut
          .map(([k, n]) => `${num(n)} ${WHY[k] || k}`).join(", ")}.`}</p>`}</div>
    </div>
    <div class="card"><h2>Latest visits</h2>
      ${d.recent.length ? html`<${Table} rows=${d.recent} initial="last" cols=${[
        { key: "last", label: "When", num: true, fmt: (v) => ago(v) },
        { key: "city", label: "Where", fmt: (v, r) => [v, r.region, r.country].filter(Boolean).join(", ") || "–" },
        { key: "device", label: "On", fmt: (v, r) => `${DEVICES[v] || v}, ${r.browser} on ${r.os}` },
        { key: "ref_host", label: "From", fmt: (v) => v || "–" },
        { key: "views", label: "Pages", num: true },
        { key: "path", label: "Path", fmt: (v) => v.split(" → ").map(page).join(" → ") }]} />` : html`<p class="muted">Nobody yet.</p>`}</div>
    <p class="muted small">Days run midnight to midnight UTC. A visitor is counted once a day: someone who comes back
      another day counts again, since nothing is kept that could recognize them. Your own devices aren't counted
      once you've signed in on them. Page views are kept ${num(d.keep_days)} days; older days keep only their totals.
      City data: <a href="https://db-ip.com" target="_blank" rel="noopener noreferrer">IP Geolocation by DB-IP</a> (CC BY 4.0).</p>
    ${box}`;
}
