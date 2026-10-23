// The APRS section: the callsigns in Settings → APRS on a map (each one's track over the chosen window and
// its newest position), and a card per callsign with what it last sent. Positions come in live from APRS-IS;
// nothing older than when the app started listening is known. Map markers and popups are built as elements,
// as on the other maps, to suit the page's CSP.
import { html, useState, useEffect, useRef, api, ago, toast, every, shade, useThemeTick, signedIn } from "./lib.js";
import { leaflet, baseMap } from "./leaflet.js";
import { Spark } from "./weather.js";

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const COLORS = ["--chart-1", "--chart-2", "--chart-3"];
const colorOf = (i) => `var(${COLORS[i % COLORS.length]})`;
const WINDOWS = [["6h", 6], ["24h", 24], ["7d", 24 * 7], ["30d", 24 * 30]];
// Stored metric (m/s, m); shown in km/h or mph, m or ft, following Settings → Display → Units.
const UNITS = { metric: { speed: ["km/h", 3.6], height: ["m", 1] }, imperial: { speed: ["mph", 2.23694], height: ["ft", 3.28084] } };
const show = (units, kind, v) => { const [label, k] = (UNITS[units] || UNITS.metric)[kind]; return v == null ? "–" : `${Math.round(v * k)} ${label}`; };
const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const heading = (deg) => (deg == null ? "" : COMPASS[Math.round((deg % 360) / 45) % 8]);
const when = (ts) => new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const place = (p) => `${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}`;
const moving = (p) => p.speed != null && p.speed > 0.5;
const aprsFi = (call) => `https://aprs.fi/info/a/${encodeURIComponent(call)}`;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function popup(call, p, units) {
  const box = el("div", "wxpop");
  const link = el("a", null, "aprs.fi →");
  link.href = aprsFi(call); link.target = "_blank"; link.rel = "noopener";
  box.append(el("b", null, call), el("div", null, `${when(p.ts)} (${ago(p.ts)})`),
    el("div", null, moving(p) ? `${show(units, "speed", p.speed)} ${heading(p.course)}` : "stopped"),
    ...(p.altitude != null ? [el("div", null, `Altitude ${show(units, "height", p.altitude)}`)] : []),
    ...(p.comment ? [el("div", "muted", p.comment)] : []), link);
  return box;
}

function AprsMap({ calls, tracks, units, gradientBy }) {
  const box = useRef(null), map = useRef(null), layer = useRef(null), fitted = useRef(false);
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

  // Redrawn whenever the positions change: a line and small dots along each track, a labelled pin at the newest.
  useEffect(() => {
    const L = window.L, group = layer.current;
    if (!ready || !group) return;
    group.clearLayers();
    const values = calls.flatMap((c) => (tracks[c.callsign] || []).map((p) => p[gradientBy])
      .filter((v) => v != null && Number.isFinite(v)));
    const lo = values.length ? values.reduce((a, v) => Math.min(a, v), Infinity) : 0;
    const hi = values.length ? values.reduce((a, v) => Math.max(a, v), -Infinity) : 1;
    const pointColor = (v, fallback) => gradientBy && v != null
      ? shade(hi > lo ? (v - lo) / (hi - lo) : 0.5) : fallback;
    const all = [];
    calls.forEach((c, i) => {
      const color = css(COLORS[i % COLORS.length]), pts = tracks[c.callsign] || [];
      const line = pts.map((p) => [p.lat, p.lon]);
      if (gradientBy) {
        for (let j = 1; j < pts.length; j++) {
          const a = pts[j - 1][gradientBy], b = pts[j][gradientBy], v = a != null && b != null ? (a + b) / 2 : a ?? b;
          L.polyline([line[j - 1], line[j]], { color: pointColor(v, color), weight: 3, opacity: 0.8 }).addTo(group);
        }
      } else if (line.length > 1) L.polyline(line, { color, weight: 3, opacity: 0.8 }).addTo(group);
      for (const p of pts.slice(0, -1)) {
        L.circleMarker([p.lat, p.lon], { radius: 3, color: pointColor(p[gradientBy], color), weight: 1, fillOpacity: 0.6 }).bindPopup(popup(c.callsign, p, units)).addTo(group);
      }
      const last = c.last;
      if (!last) return;
      const face = el("div", "aprspin"), arrow = el("i");
      face.style.borderColor = color;
      arrow.style.borderBottomColor = color;
      arrow.hidden = !moving(last) || last.course == null;
      if (!arrow.hidden) arrow.style.transform = `rotate(${last.course}deg)`;
      face.append(arrow, el("b", null, c.callsign));
      L.marker([last.lat, last.lon], { icon: L.divIcon({ html: face, className: "wxpin-wrap", iconSize: null, iconAnchor: [14, 14] }), zIndexOffset: 1000 })
        .bindPopup(popup(c.callsign, last, units)).addTo(group);
      all.push(...line, [last.lat, last.lon]);
    });
    // Fit everything into view the first time anything is known.
    if (!fitted.current && all.length) {
      fitted.current = true;
      map.current.fitBounds(all, { padding: [60, 60], maxZoom: 13 });
    }
  }, [ready, calls, tracks, units, gradientBy, theme]);
  return html`<div class="mapwrap aprsmap" ref=${box}></div>`;
}

// What a symbol (table + code) is, for the common ones; the rest show as their code.
const SYMBOLS = { "/>": "car", "/k": "truck", "/u": "semi", "/v": "van", "/j": "jeep", "/R": "RV", "/U": "bus", "/<": "motorcycle",
  "/b": "bicycle", "/[": "on foot", "/'": "small aircraft", "/^": "aircraft", "/X": "helicopter", "/O": "balloon", "/s": "boat",
  "/Y": "yacht", "/-": "home", "/y": "home (yagi)", "/_": "weather station", "/#": "digipeater", "/&": "gateway", "/r": "repeater",
  "/a": "ambulance", "/f": "fire truck", "/=": "train", "\\n": "node", "/I": "TCP/IP" };
const symbolName = (sym) => SYMBOLS[sym] || (sym ? `symbol ${sym}` : "");
// The 6-character Maidenhead grid square (e.g. EM10dg), as hams give their location.
export function grid(lat, lon) {
  const x = lon + 180, y = lat + 90, A = (n) => String.fromCharCode(65 + n), a = (n) => String.fromCharCode(97 + n);
  return A(Math.floor(x / 20)) + A(Math.floor(y / 10)) + Math.floor((x % 20) / 2) + Math.floor(y % 10)
    + a(Math.floor((x % 2) * 12)) + a(Math.floor((y % 1) * 24));
}
const km = (p, q) => {          // great-circle distance
  const r = Math.PI / 180, dLat = (q.lat - p.lat) * r, dLon = (q.lon - p.lon) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(p.lat * r) * Math.cos(q.lat * r) * Math.sin(dLon / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(h));
};
const dist = (units, k) => (units === "imperial" ? `${(k * 0.621371).toFixed(k < 16 ? 1 : 0)} mi` : `${k.toFixed(k < 10 ? 1 : 0)} km`);
const span = (s) => (s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
const median = (xs) => { const v = [...xs].sort((x, y) => x - y); return v.length ? v[v.length >> 1] : null; };

// The track over the window, summed up: how far, how fast at most, how often it beacons, and series for the sparklines.
function summary(track) {
  let d = 0;
  for (let i = 1; i < track.length; i++) d += km(track[i - 1], track[i]);
  const gaps = track.slice(1).map((q, i) => q.ts - track[i].ts);
  const speeds = track.map((q) => q.speed), alts = track.map((q) => q.altitude);
  return { n: track.length, km: d, top: Math.max(0, ...speeds.filter((v) => v != null)), every: median(gaps),
           speeds, alts: alts.some((v) => v != null) ? alts : null };
}

// Which way it's going and how fast, in a small dial; a dot in the middle when it's standing still.
function Course({ p, units, color }) {
  const go = moving(p) && p.course != null;
  const [label, k] = (UNITS[units] || UNITS.metric).speed;
  return html`<svg class="aprsdial" viewBox="-50 -50 100 100" role="img" aria-label=${go ? `Heading ${heading(p.course)}` : "Stopped"}>
    <circle r="44" fill="none" stroke="var(--line)" stroke-width="2" />
    ${Array.from({ length: 8 }, (_, i) => html`<line key=${i} y1="-44" y2=${i % 2 ? "-40" : "-36"} stroke="var(--muted)" stroke-width="1.2" transform=${`rotate(${i * 45})`} />`)}
    <text y="-26" text-anchor="middle" dominant-baseline="central" font-size="9" fill="var(--muted)">N</text>
    ${go ? html`<g transform=${`rotate(${p.course})`}><path d="M0,-34 L8,-18 L0,-22 L-8,-18 Z" fill=${color} /></g>
      <text y="4" text-anchor="middle" font-size="20" font-weight="600" fill="var(--fg)">${Math.round(p.speed * k)}</text>
      <text y="17" text-anchor="middle" font-size="9" fill="var(--muted)">${label} ${heading(p.course)}</text>`
      : html`<circle r="6" fill=${color} /><text y="22" text-anchor="middle" font-size="9" fill="var(--muted)">stopped</text>`}
  </svg>`;
}

const Stat = ({ label, value, note, wide }) => html`<div class=${"aprsstat" + (wide ? " wide" : "")}><span>${label}</span><b title=${value}>${value}</b>${note && html`<small title=${note}>${note}</small>`}</div>`;

function Packets({ call, units }) {
  const [rows, setRows] = useState(null);
  useEffect(() => { api(`/api/aprs/packets?callsign=${encodeURIComponent(call)}&limit=25`).then((x) => setRows(x.packets)).catch((e) => toast(e.message)); }, [call]);
  if (!rows) return html`<p class="muted">Loading…</p>`;
  return html`<div class="aprspackets"><table><tbody>
    ${rows.map((p) => html`<tr key=${p.ts} title=${p.raw}><td>${when(p.ts)}</td><td class="mono">${grid(p.lat, p.lon)}</td>
      <td>${moving(p) ? `${show(units, "speed", p.speed)} ${heading(p.course)}` : "–"}</td><td class="muted">${p.comment || ""}</td></tr>`)}
  </tbody></table></div>`;
}

function CallCard({ c, i, units, now, track, win }) {
  const [open, setOpen] = useState(false);
  const p = c.last, color = colorOf(i);
  const age = p ? now - p.ts : null;
  const [fresh, freshLabel] = age == null ? ["s-none", "not heard"] : age < 900 ? ["s-applied", "live"] : age < 3 * 3600 ? ["s-pending", "recent"] : ["s-none", "quiet"];
  const t = summary(track || []);
  return html`<div class=${"card aprscard" + (fresh === "s-none" ? " quiet" : "")}>
    <div class="aprshead">
      <span class="aprsdot" style=${`background: ${color}`}></span>
      <b class="aprscall">${c.callsign}</b>
      ${p && html`<span class="muted aprswhat">${[symbolName(p.symbol), grid(p.lat, p.lon)].filter(Boolean).join(" · ")}</span>`}
      <span class=${"status " + fresh}>${freshLabel}${p ? ` · ${ago(p.ts)}` : ""}</span>
    </div>
    ${!p ? html`<p class="muted">Not heard yet. It shows up the next time it beacons.</p>` : html`
      <div class="aprsbody">
        <${Course} p=${p} units=${units} color=${color} />
        <div class="aprsstats">
          <${Stat} label="Altitude" value=${show(units, "height", p.altitude)} />
          <${Stat} label="Beacons" value=${t.every ? `every ${span(t.every)}` : "–"} note=${`${t.n} in ${win}`} />
          <${Stat} label="Travelled" value=${t.n > 1 ? dist(units, t.km) : "–"} note=${`in ${win}`} />
          <${Stat} label="Top speed" value=${t.top > 0.5 ? show(units, "speed", t.top) : "–"} note=${`in ${win}`} />
          <${Stat} label="Position" value=${place(p)} note=${when(p.ts)} wide />
          <${Stat} label="Kept" value=${`${c.positions} positions`} note=${`since ${when(c.first_ts)}`} />
        </div>
      </div>
      ${t.n > 1 && html`<div class="aprssparks">
        <div><span>Speed</span><${Spark} values=${t.speeds} lo=${0} color=${COLORS[i % COLORS.length]} gradient /></div>
        ${t.alts && html`<div><span>Altitude</span><${Spark} values=${t.alts} color=${COLORS[i % COLORS.length]} gradient /></div>`}
      </div>`}
      ${p.comment && html`<p class="aprscomment">${p.comment}</p>`}
      <div class="aprsfoot">
        ${p.path && html`<span class="aprspath" title="The path this packet took">${p.path.split(",").map((h, j) => html`<code key=${j}>${h}</code>`)}</span>`}
        <span class="aprslinks"><a href=${aprsFi(c.callsign)} target="_blank" rel="noopener">aprs.fi ↗</a>
          <button class="link" onClick=${() => setOpen(!open)}>${open ? "Hide packets" : "Packets"}</button></span>
      </div>
      ${open && html`<${Packets} call=${c.callsign} units=${units} />`}`}
  </div>`;
}

export function Aprs() {
  const [d, setD] = useState(null);
  const [hours, setHours] = useState(24);
  const [gradientBy, setGradientBy] = useState("speed");
  const [tracks, setTracks] = useState({});
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    const load = () => api("/api/aprs/stations")
      .then((x) => { if (alive) { setD(x); setErr(""); } }).catch((e) => alive && setErr(e.message));
    const stop = every(load, 15000);
    return () => { alive = false; stop(); };
  }, []);
  // Refresh tracks with the station list so beacon frequency keeps up while the page is open.
  const calls = d ? d.callsigns.map((c) => c.callsign).join(",") : "";
  useEffect(() => {
    if (!d) return;
    const names = calls ? calls.split(",") : [];
    let alive = true;
    const load = () => Promise.all(names.map((call) => api(`/api/aprs/track?callsign=${encodeURIComponent(call)}&hours=${hours}`)
      .then((x) => [call, x.points]).catch(() => [call, []])))
      .then((pairs) => alive && setTracks(Object.fromEntries(pairs)));
    const stop = every(load, 15000);
    return () => { alive = false; stop(); };
  }, [calls, hours]);

  if (!d) return err ? html`<p class="problem">${err}</p>` : html`<div class="boot">Loading…</div>`;
  if (!d.callsigns.length) return html`<div class="card"><h2>No callsigns yet</h2>
    ${signedIn() && html`<p>Add the callsigns to follow under <a href="#/settings/aprs">Settings → APRS</a>.</p>`}</div>`;
  return html`
    <div class="toolbar">
      <span class="seg" aria-label="Track window">${WINDOWS.map(([label, h]) => html`<button key=${h}
        class=${h === hours ? "primary" : ""} onClick=${() => setHours(h)}>${label}</button>`)}</span>
      <label class="muted small">Track color
        <select aria-label="Track color" value=${gradientBy} onChange=${(e) => setGradientBy(e.target.value)}>
          <option value="">Callsign</option><option value="speed">Speed</option><option value="altitude">Altitude</option>
        </select>
      </label>
    </div>
    <${AprsMap} calls=${d.callsigns} tracks=${tracks} units=${d.units} gradientBy=${gradientBy} />
    <div class="cols aprscols">${d.callsigns.map((c, i) => html`<${CallCard} key=${c.callsign} c=${c} i=${i} units=${d.units} now=${d.now}
      track=${tracks[c.callsign]} win=${WINDOWS.find((w) => w[1] === hours)[0]} />`)}</div>`;
}
