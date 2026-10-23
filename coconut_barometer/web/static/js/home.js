// The home page (#/): a large link per public section, each with a glance at what's there now (the newest
// photo, each station's weather, when each tracked callsign was last heard); signed in, the private pages
// in a row below. Each glance loads on its own, and a card whose glance can't load keeps its description.
import { html, useState, useEffect, api, num, ago, pick, every, shade, useThemeTick } from "./lib.js";
import { show, compass } from "./weather.js";

function useGlance(path, ms) {
  const [d, setD] = useState(null);
  useEffect(() => {
    let live = true;
    const load = () => api(path).then((x) => live && setD(x)).catch(() => {});
    const stop = ms ? every(load, ms) : (load(), () => {});
    return () => { live = false; stop(); };
  }, [path]);
  return d;
}

// Hourly temperatures over the day before a station's newest reading, so one that has stopped reporting
// shows its last day (beside how long ago that was). Fetched again once per hour of new data.
function useDay(st) {
  const ts = st?.obs?.ts, dev = st?.device_id, hour = ts ? Math.floor(ts / 3600) : null;
  const [pts, setPts] = useState(null);
  useEffect(() => {
    if (!hour) return;
    let live = true;
    api(`/api/wx/days?device=${dev}&start=${ts - 86400}&end=${ts}`).then((d) => live && setPts(
      d.days.flatMap((x) => x.hours.map((v, h) => [x.t + h * 3600, v])).filter(([t, v]) => v != null && t > ts - 90000 && t <= ts)))
      .catch(() => {});
    return () => { live = false; };
  }, [dev, hour]);
  return pts;
}

// That day as a soft area along the bottom of the card, colored cold to warm along the
// Settings → Display → Colors gradient (each point by its own temperature).
function DayCurve({ pts }) {
  useThemeTick();
  if (!pts || pts.length < 2) return null;
  const vs = pts.map(([, v]) => v), lo = Math.min(...vs), hi = Math.max(...vs), t0 = pts[0][0], t1 = pts[pts.length - 1][0];
  const share = (v) => (hi > lo ? (v - lo) / (hi - lo) : 0.5);
  const at = ([t, v]) => [((t - t0) / (t1 - t0)) * 100, 38 - share(v) * 28];
  const line = pts.map((p, i) => `${i ? "L" : "M"}${at(p).map((n) => n.toFixed(1)).join(",")}`).join("");
  return html`<svg class="homebg curve" viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true">
    <defs><linearGradient id="home-day" x1="0" x2="1" y1="0" y2="0">
      ${pts.map((p, i) => html`<stop key=${i} offset=${(at(p)[0] / 100).toFixed(3)} stop-color=${shade(share(p[1]))} />`)}
    </linearGradient></defs>
    <path class="fill" d=${`${line}L100,40L0,40Z`} fill="url(#home-day)" />
    <path class="edge" d=${line} stroke="url(#home-day)" vector-effect="non-scaling-stroke" />
  </svg>`;
}

// Signal rings from the corner, like the radio waves in the logo: no map, so no position on the front page.
const Rings = () => html`<svg class="homebg rings" viewBox="0 0 200 200" aria-hidden="true">
  ${[40, 80, 120, 160].map((r) => html`<circle key=${r} cx="200" cy="200" r=${r} />`)}<circle class="dot" cx="200" cy="200" r="10" />
</svg>`;

function Section({ href, title, line, image, bg, children }) {
  return html`<a class=${"homecard" + (image ? " pic" : "")} href=${href}>
    ${image && html`<img src=${image} alt="" decoding="async" />`}${bg}
    <h2>${title}</h2>
    <div class="glance">${children}</div>
    <small>${line}</small></a>`;
}

function PhotosCard() {
  const d = useGlance("/api/photos?limit=1");
  const p = d?.photos[0];
  return html`<${Section} href="#/photos" title="Photos" image=${p && pick(p.sizes, ["c", "z", "w"])}
    line="The photostream, albums and the map">
    ${d && html`<div class="glance-row"><b>${num(d.total)}</b><span>photos</span></div>
      ${p?.title && html`<span>Newest: ${p.title}</span>`}`}<//>`;
}

function WeatherCard() {
  const d = useGlance("/api/wx/live", 60000);
  const shown = (d?.stations || []).filter((s) => s.obs);
  const day = useDay(shown[0]);
  return html`<${Section} href="#/weather" title="Weather" line="Live readings, history and the radar map"
    bg=${html`<${DayCurve} pts=${day} />`}>
    ${shown.map((s) => html`<div class="glance-row" key=${s.device_id}>
      <b>${show(d.units, "temp", s.obs.temp, 0)}</b>
      <span>${s.obs.wind_avg > 0 ? `wind ${show(d.units, "speed", s.obs.wind_avg, 0)} ${compass(s.obs.wind_dir)}`.trim() : "calm"}</span>
      <span class="muted">${s.name} · ${ago(s.obs.ts)}</span></div>`)}<//>`;
}

function AprsCard() {
  const d = useGlance("/api/aprs/stations", 60000);
  return html`<${Section} href="#/aprs" title="APRS" line="Positions heard on APRS-IS, and the igate's reach" bg=${html`<${Rings} />`}>
    ${(d?.callsigns || []).map((c) => html`<div class="glance-row" key=${c.callsign}>
      <b>${c.callsign}</b><span class="muted">${c.last ? `heard ${ago(c.last.ts)}` : "not heard yet"}</span></div>`)}<//>`;
}

export function Home({ session, pending, queued }) {
  return html`<div class="home">
    <header class="home-head">
      <img class="logo hero" src="/static/img/logo-320.png" width="200" height="195" alt="" />
      <h1 class="site-name">Coconut Barometer</h1>
      <p class="muted">Photos, live weather and APRS radio, in one place.</p>
    </header>
    <nav class="homecards" aria-label="Sections">
      <${PhotosCard} /><${WeatherCard} /><${AprsCard} />
    </nav>
    ${session && html`<nav class="homelinks" aria-label="Signed in">
      <a href="#/photos/review">Review${pending ? html`<span class="badge">${pending}</span>` : ""}</a>
      <a href="#/photos/analytics">Analytics</a>
      <a href="#/queue">Queue<span class=${"qdot" + (queued ? " busy" : "")} aria-hidden="true"></span></a>
      <a href="#/settings">Settings</a></nav>`}
  </div>`;
}
