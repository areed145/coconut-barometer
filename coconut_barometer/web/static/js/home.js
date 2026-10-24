// The home page (#/): a large link per public section, each with a glance at what's there now (the newest
// photo, each station's weather, when each tracked callsign was last heard; signed in, the radar too); signed in, the private pages
// in a row below. Each glance loads on its own, and a card whose glance can't load keeps its description.
import { html, useState, useEffect, api, num, ago, pick, every, shade, useThemeTick, isDark, today, dayLabel, photosLink, HealthPanel,
  StatusDot } from "./lib.js";
import { show, compass } from "./units.js";

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

// image: a photo behind the card's text; media: a picture below it, shown whole (the radar, whose time and credits
// are at its edges).
function Section({ href, title, line, image, media, srcset, alt = "", bg, children }) {
  return html`<a class=${"homecard" + (image ? " pic" : media ? " wide" : "")} href=${href}>
    ${image && html`<img src=${image} alt="" decoding="async" />`}${bg}
    <h2>${title}</h2>
    <div class="glance">${children}</div>
    <small>${line}</small>
    ${media && html`<img class="media" src=${media} srcset=${srcset} alt=${alt} width="640" height="360" decoding="async" />`}</a>`;
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

// The newest radar around the stations, drawn on the server (weather/snapshot.py) in the page's light or dark; signed
// in only, as the image is. Each frame has its own address (?t=), so the browser fetches each frame's picture once.
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
function RadarCard() {
  useThemeTick();
  const d = useGlance("/api/wx/radar/glance", 120000);
  if (!d?.stations.length) return null;
  const src = (scale) => `/api/wx/radar/snapshot.png?theme=${isDark() ? "dark" : "light"}&scale=${scale}&t=${d.t || 0}`;
  const n = d.nearest, around = d.stations.join(", ");
  return html`<${Section} href="#/weather/map" title="Radar" media=${src(1)} srcset=${`${src(1)} 1x, ${src(2)} 2x`}
    alt=${d.fresh ? `Radar at ${clock(d.t)} around ${around}` : `Radar unavailable around ${around}`}
    line="The latest radar and storm cells around the stations">
    <div class="glance-row"><b>${d.fresh ? `radar ${clock(d.t)}` : "radar unavailable"}</b></div>
    ${d.fresh && n && html`<div class="glance-row"><span>${n.km < 3 ? `raining at ${n.station}`
      : `rain ${show(d.units, "dist", n.km, 0)} ${compass(n.toward)}`}</span></div>`}
    <div class="glance-row"><span>${d.storms ? `${d.storms} storm${d.storms === 1 ? "" : "s"} within ${show(d.units, "dist", d.storm_km, 0)}`
      : "no storms nearby"}</span></div><//>`;
}

function AprsCard() {
  const d = useGlance("/api/aprs/stations", 60000);
  return html`<${Section} href="#/aprs" title="APRS" line="Positions heard on APRS-IS, and the igate's reach" bg=${html`<${Rings} />`}>
    ${(d?.callsigns || []).map((c) => html`<div class="glance-row" key=${c.callsign}>
      <b>${c.callsign}</b><span class="muted">${c.last ? `heard ${ago(c.last.ts)}` : "not heard yet"}</span></div>`)}<//>`;
}

// Highlights: running totals and streaks that mean something from the first week of history, each linking to
// where it comes from. "Record" only once there's more than a year to compare with (the server decides).
function Hl({ href, label, value, note, record }) {
  return html`<a class="wxtile hl" href=${href}><span>${label}${record ? html` <i class="hl-record">record</i>` : ""}</span>
    <b>${value}</b><small>${note || " "}</small></a>`;
}

function Highlights() {
  const d = useGlance("/api/highlights", 300000);
  if (!d) return null;
  const { units: u, weather: w, igate: g, photos: p } = d;
  const rainDigits = u === "imperial" ? 2 : 1;
  const tz = { timeZone: w?.timezone || undefined };
  const date = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { ...tz, month: "short", day: "numeric" });
  const isoDate = (iso) => date(new Date(iso + "T12:00:00Z").getTime() / 1000);
  const monthName = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { ...tz, month: "long" });
  const tiles = [];
  if (w) {
    const r = w.rain, m = w.month, y = w.year, since = w.since > y.start ? ` (since ${date(w.since)})` : "";
    tiles.push(r.today >= 0.254 ? html`<${Hl} key="rain" href="#/weather/history?range=24h&chart=rain" label="Rain today"
        value=${show(u, "rain", r.today, rainDigits)} note=${w.name} />`
      : html`<${Hl} key="rain" href="#/weather/history?range=30d&chart=rain" label="Since it rained"
        value=${r.last_day ? (r.days_since === 1 ? "1 day" : `${r.days_since} days`) : "no rain yet"}
        note=${r.last_day ? `${isoDate(r.last_day)} · ${show(u, "rain", r.last_amount, rainDigits)}` : `since ${date(w.since)}`} />`);
    tiles.push(html`<${Hl} key="month" href="#/weather/records" label=${`${monthName(m.start)} so far`}
      value=${`${show(u, "rain", m.rain, rainDigits)} rain`} record=${w.record.length > 0}
      note=${[m.high && `high ${show(u, "temp", m.high.v, 0)} ${date(m.high.ts)}`, m.low && `low ${show(u, "temp", m.low.v, 0)} ${date(m.low.ts)}`].filter(Boolean).join(" · ")} />`);
    const warm = (() => { const mo = new Date(w.as_of * 1000).toLocaleDateString("en-US", { ...tz, month: "numeric" }); return mo >= 4 && mo <= 10; })();
    const year = new Date(w.as_of * 1000).toLocaleDateString("en-US", { ...tz, year: "numeric" });
    tiles.push(warm ? html`<${Hl} key="season" href="#/weather/records" label=${`Days ${u === "imperial" ? "≥ 100 °F" : "≥ 37.8 °C"} in ${year}`}
        value=${num(y.hot_days)} note=${`highs that hot${since}`} />`
      : html`<${Hl} key="season" href="#/weather/records" label=${`Freezing nights in ${year}`}
        value=${num(y.freezing_nights)} note=${`lows of ${u === "imperial" ? "32 °F" : "0 °C"} or below${since}`} />`);
    const l = w.lightning;
    tiles.push(html`<${Hl} key="strike" href="#/weather/history?range=30d&chart=lightning"
      label=${`Lightning within ${u === "imperial" ? "10 mi" : "16 km"}`} value=${l.last_near ? date(l.last_near.ts) : "none yet"}
      note=${`${num(m.strikes)} strike${m.strikes === 1 ? "" : "s"} in ${monthName(m.start)}`} />`);
  }
  if (g?.farthest) tiles.push(html`<${Hl} key="igate" href="#/aprs/igate" label="Farthest heard this month"
    value=${show(u, "dist", g.farthest.km, 0)} note=${`${g.farthest.callsign} · ${date(g.farthest.ts)}`} />`);
  if (p) {
    tiles.push(html`<${Hl} key="taken" href=${photosLink({ year: p.year, sort: "taken" })} label=${`Photos taken in ${p.year}`}
      value=${num(p.taken)} note="see them, newest first" />`);
    if (p.top_upload) tiles.push(html`<${Hl} key="top" href=${`#/photos/photo/${p.top_upload.id}`} label="Top upload this month"
      value=${`${num(p.top_upload.views)} views`} note=${p.top_upload.title || "Untitled"} />`);
  }
  if (!tiles.length) return null;
  const stale = w && Date.now() / 1000 - w.as_of > 3 * 3600;
  return html`<section class="highlights" aria-label="Highlights">
    <h2 class="section">Highlights${stale ? html` <span class="muted">weather as of ${date(w.as_of)}</span>` : ""}</h2>
    <div class="hltiles">${tiles}</div></section>`;
}

// Photos taken on today's date in earlier years (not this year's: those are just "recent").
function OnThisDay() {
  const on = today(), year = new Date().getFullYear();
  const d = useGlance(`/api/photos?on=${on}&sort=taken&limit=40`);
  const past = (d?.photos || []).filter((p) => +p.date_taken.slice(0, 4) < year).slice(0, 8);
  if (!past.length) return null;
  const years = (p) => { const n = year - +p.date_taken.slice(0, 4); return n === 1 ? "1 year ago" : `${n} years ago`; };
  return html`<section class="onthisday" aria-label="On this day">
    <h2 class="section">On this day <span class="muted">${dayLabel(on)}</span>
      <a href=${photosLink({ on, sort: "taken" })}>See all</a></h2>
    <div class="otd-strip">${past.map((p) => html`<a key=${p.id} href=${`#/photos/photo/${p.id}`} title=${p.title || "Untitled"}>
      <img src=${pick(p.sizes, ["q", "n", "m"])} alt=${p.title || ""} loading="lazy" decoding="async" /><span>${years(p)}</span></a>`)}</div>
  </section>`;
}

export function Home({ session, pending, queued, health }) {
  return html`<div class="home">
    <header class="home-head">
      <img class="logo hero" src="/static/img/logo-320.png" width="200" height="195" alt="" />
      <h1 class="site-name">Coconut Barometer</h1>
      <p class="muted">Photos, live weather and APRS radio, in one place.</p>
    </header>
    <nav class="homecards" aria-label="Sections">
      <${PhotosCard} /><${WeatherCard} />${session && html`<${RadarCard} />`}<${AprsCard} />
    </nav>
    <${HealthPanel} health=${health} fix=${session} />
    <${Highlights} />
    <${OnThisDay} />
    ${session && html`<nav class="homelinks" aria-label="Signed in">
      <a href="#/photos/review">Review${pending ? html`<span class="badge">${pending}</span>` : ""}</a>
      <a href="#/photos/analytics">Analytics</a>
      <a href="#/visitors">Visitors</a>
      <a href="#/status">Status<${StatusDot} health=${health} queued=${queued} /></a>
      <a href="#/settings">Settings</a></nav>`}
  </div>`;
}
