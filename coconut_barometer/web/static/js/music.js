// Music (#/music): Apple Music listening, read by the server every hour (apple_music.py). Days and hours are in this
// browser's timezone; the times are estimates, since Apple doesn't say when a track was played.
import { html, useState, useEffect, useMemo, useRef, api, num, ago, toast } from "./lib.js";
import { Chart } from "./chart.js";
import { Kpi, Bars, Heatmap, Range } from "./viz.js";
import { MusicGraph } from "./music_graph.js";

const ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const HOURS = [...Array(24).keys()].map(String);
const hours = (minutes) => (minutes >= 600 ? Math.round(minutes / 60) : Math.round(minutes / 6) / 10);
const when = (ts) => new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

const art = (id, size = 160) => `/api/music/art/${encodeURIComponent(id)}?s=${size}`;
const hide = (e) => { e.target.style.visibility = "hidden"; };   // the tile's color stays if a cover won't load
const cover = (r, size, cls) => r?.art && html`<img class=${cls} loading="lazy" alt="" src=${art(r.art, size)} style=${r.bg ? `background:#${r.bg}` : ""} onError=${hide} />`;
const named = (r) => r.label.split(" · ")[0];
const RANGE_NAME = { 7: "the last 7 days", 30: "the last 30 days", 90: "the last 90 days", 365: "the last year", 3650: "all time" };

function Top({ title, rows, covers = true }) {
  return html`<div class="card"><h2>${title}</h2><${Bars} rows=${rows} value="plays" empty="Nothing yet." thumb=${covers ? (r) => cover(r, 64, "thumb") : null} /></div>`;
}

// A band of color per day, or per week over long ranges: each one's covers blended (music_insights.py).
function ColorStrip({ bands }) {
  const ref = useRef(null);
  const [at, setAt] = useState(null);
  useEffect(() => {
    const c = ref.current;
    if (!c || !bands.length) return;
    c.width = bands.length; c.height = 1;
    const ctx = c.getContext("2d");
    bands.forEach((b, i) => { ctx.fillStyle = `#${b.color}`; ctx.fillRect(i, 0, 1, 1); });
  }, [bands]);
  if (!bands.length) return html`<div class="card"><h2>Your listening in color</h2><p class="muted">The colors come from Apple's catalog, a few hundred tracks a minute.</p></div>`;
  const move = (e) => { const box = e.target.getBoundingClientRect(); setAt(bands[Math.min(bands.length - 1, Math.floor(((e.clientX - box.left) / box.width) * bands.length))]); };
  return html`<div class="card"><h2>Your listening in color</h2>
    <canvas class="colorstrip" ref=${ref} style="image-rendering: pixelated" role="img" aria-label="Each band is the blended cover colors of what was played then"
      onMouseMove=${move} onMouseLeave=${() => setAt(null)}></canvas>
    <div class="colorstrip-key"><span>${bands[0].start}</span><span>${at ? `${at.start} · ${num(at.plays)} plays` : "each band blends that period's album covers"}</span><span>${bands[bands.length - 1].start}</span></div></div>`;
}

function AlbumWall({ albums }) {
  const [open, setOpen] = useState(null);
  if (!albums.length) return null;
  const chosen = albums.find((a) => a.label === open);
  return html`<div class="card"><h2>Album wall</h2>
    <div class="wall">${albums.map((a, i) => html`<button key=${a.label} class=${i === 0 ? "big" : i < 5 ? "mid" : ""} aria-pressed=${a.label === open}
        title=${`${a.label} · ${num(a.plays)} plays`} aria-label=${a.label} style=${a.bg ? `background:#${a.bg}` : ""}
        onClick=${() => setOpen(a.label === open ? null : a.label)}><img loading="lazy" alt="" src=${art(a.art, i < 5 ? 320 : 160)} onError=${hide} /></button>`)}</div>
    ${chosen && html`<h3>${chosen.label}</h3><${Bars} rows=${chosen.songs.map((x) => ({ label: x.name, plays: x.plays }))} value="plays" />`}</div>`;
}

function Palettes({ months }) {
  if (!months.length) return null;
  const name = (m) => new Date(`${m}-15T12:00:00`).toLocaleDateString([], { month: "short", year: "numeric" });
  return html`<div class="card"><h2>Monthly palette</h2><div class="palettes">${months.map((m) => html`<div class="palette" key=${m.month}>
    ${cover(m.album, 160, "") || html`<span></span>`}<b>${name(m.month)}</b>
    <span class="swatches">${m.colors.map((c) => html`<i key=${c.color} style=${`flex:${c.share};background:#${c.color}`}></i>`)}</span>
    <span class="name">${m.album ? named(m.album) : ""}</span></div>`)}</div></div>`;
}

// Rainy days against dry ones, at the Tempest stations (music_insights.py).
function RainOrShine({ days }) {
  const [d, setD] = useState(null);
  useEffect(() => { setD(null); api("/api/music/weather", { params: { days, tz: ZONE } }).then(setD).catch(() => {}); }, [days]);
  if (!d?.enough) return null;
  const list = (title, rows) => html`<div><h3>${title}</h3><${Bars} rows=${rows} value="lift" right=${(r) => `${r.lift}×`} title=${(r) => `${num(r.rainy)} on rainy days · ${num(r.dry)} on dry days`} empty="Nothing stands out." /></div>`;
  return html`<div class="card"><h2>Rain or shine</h2>
    <p class="muted small">${num(d.plays)} plays on ${num(d.rainy_days)} rainy days (1 mm or more at a station), against dry days. Each bar is how many times more likely it is that way.</p>
    <div class="cols">${list("More on rainy days", d.rainy)}${list("More on dry days", d.dry)}</div>
    ${(d.rainy_genres.length > 0 || d.dry_genres.length > 0) && html`<div class="cols">${list("Rainy genres", d.rainy_genres)}${list("Dry genres", d.dry_genres)}</div>`}</div>`;
}

const loadImage = (src) => new Promise((ok) => { const i = new Image(); i.onload = () => ok(i); i.onerror = () => ok(null); i.src = src; });
const light = (hex) => { const n = parseInt(hex, 16); return (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) > 140; };

// A picture of the period, drawn in the browser from what's on the page, colored by its top album.
async function drawPoster(canvas, d, days) {
  const W = 1080, H = 1350, M = 72, album = d.albums[0];
  canvas.width = W; canvas.height = H;
  const c = canvas.getContext("2d"), bg = album?.bg || "1a1a1a", ink = album?.text && album.bg ? `#${album.text}` : light(bg) ? "#111111" : "#ffffff";
  c.fillStyle = `#${bg}`; c.fillRect(0, 0, W, H);
  await document.fonts.ready;
  const font = (size, weight = 500) => { c.font = `${weight} ${size}px "DM Sans", sans-serif`; };
  const fit = (text, width) => { let t = text; while (c.measureText(t).width > width && t.length > 4) t = `${t.slice(0, -2).trimEnd()}…`; return t; };
  c.fillStyle = ink; c.globalAlpha = 0.75; font(34); c.fillText(`My listening, ${RANGE_NAME[days] || `the last ${days} days`}`, M, 104); c.globalAlpha = 1;
  const image = album?.art && await loadImage(art(album.art, 640));
  if (image) c.drawImage(image, M, 136, 480, 480);
  const stat = (y, value, label) => { font(76, 700); c.fillText(value, 600, y); font(30); c.globalAlpha = 0.75; c.fillText(label, 600, y + 42); c.globalAlpha = 1; };
  stat(214, num(d.totals.plays), "plays"); stat(374, num(hours(d.totals.minutes)), "hours"); stat(534, num(d.totals.artists), "artists");
  const column = (x, title, rows) => {
    font(30, 700); c.fillText(title, x, 700);
    rows.slice(0, 5).forEach((r, i) => { font(30); c.fillText(fit(`${i + 1}. ${named(r)}`, 440), x, 756 + i * 54); });
  };
  column(M, "Artists", d.artists); column(600, "Songs", d.tracks);
  const y = 1080, h = 170, w = (W - 2 * M) / Math.max(1, d.strip.length);
  d.strip.forEach((b, i) => { c.fillStyle = `#${b.color}`; c.fillRect(M + i * w, y, Math.ceil(w), h); });
  c.fillStyle = ink; c.globalAlpha = 0.6; font(26); c.fillText("Each band is that period's album covers, blended", M, 1296); c.globalAlpha = 1;
}

function Poster({ d, days, onClose }) {
  const ref = useRef(null);
  useEffect(() => { drawPoster(ref.current, d, days).catch((e) => toast(e.message)); }, [d, days]);
  const save = () => ref.current.toBlob((blob) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `music-${days === 3650 ? "all-time" : `${days}-days`}.png`; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  return html`<div class="card poster"><h2>Poster</h2><canvas ref=${ref} role="img" aria-label="A poster of this period's listening"></canvas>
    <button class="primary" onClick=${save}>Download</button> <button onClick=${onClose}>Close</button></div>`;
}

function Connection({ s, empty }) {
  if (!s.keys_set || !s.connected) return html`<p class="problem">Apple Music isn't connected: <a href="#/settings/music">Settings → Music</a>.</p>`;
  if (s.error) return html`<p class="problem">${s.error} <a href="#/settings/music">${s.reconnect ? "Connect again" : "Settings"}</a></p>`;
  if (!s.started) return html`<p class="muted">Connected. The first read is within a minute or two.</p>`;
  if (empty) return html`<p class="muted">Connected. Apple doesn't say when the tracks already on its list were played, so counting starts with what you play from now on,
    shown after the next read. Your earlier history can be added in <a href="#/settings/music">Settings → Music</a>.</p>`;
  return null;
}

export function Music() {
  const [days, setDays] = useState(30);
  const [d, setD] = useState(null);
  const [poster, setPoster] = useState(false);
  useEffect(() => { setD(null); api("/api/music/analytics", { params: { days, tz: ZONE } }).then(setD).catch((e) => setD({ error: e.message })); }, [days]);
  const range = html`<${Range} days=${days} setDays=${setDays} />`;
  const series = useMemo(() => (d?.series ? [{ label: "plays", values: d.series.map((r) => r.plays), color: "--chart-1", bars: true }] : []), [d]);
  if (!d) return html`<div class="toolbar">${range}</div><div class="boot">Loading…</div>`;
  if (d.error) return html`<div class="toolbar">${range}</div><p class="problem">${d.error}</p>`;
  const t = d.totals, p = d.previous, s = d.state;
  return html`<div class="toolbar">${range}<button onClick=${() => setPoster(!poster)}>Poster</button></div>
    ${poster && html`<${Poster} d=${d} days=${days} onClose=${() => setPoster(false)} />`}
    <${Connection} s=${s} empty=${!t.plays && !s.imported.plays} />
    <div class="kpis">
      <${Kpi} label="Plays" now=${t.plays} before=${p?.plays} />
      <${Kpi} label="Hours" now=${hours(t.minutes)} before=${p && hours(p.minutes)} note=${s.imported.plays ? "as heard, in the export" : "if each played to the end"} />
      <${Kpi} label="Songs" now=${t.tracks} before=${p?.tracks} />
      <${Kpi} label="Artists" now=${t.artists} before=${p?.artists} />
    </div>
    <${ColorStrip} bands=${d.strip} />
    <${AlbumWall} albums=${d.wall} />
    <div class="card"><h2>Plays per day</h2><${Chart} days=${d.series.map((r) => r.day)} series=${series} /></div>
    <div class="cols"><${Top} title="Top artists" rows=${d.artists} /><${Top} title="Top songs" rows=${d.tracks} /></div>
    <div class="cols"><${Top} title="Top albums" rows=${d.albums} /><${Top} title="Genres" rows=${d.genres} covers=${false} /></div>
    <${Palettes} months=${d.palettes} />
    <div class="card"><h2>When you listen</h2><${Heatmap} cells=${d.heat} rows=${WEEK} cols=${HOURS} unit="plays" /></div>
    <${MusicGraph} days=${days} />
    <${RainOrShine} days=${days} />
    <div class="card"><h2>Latest plays</h2>
      ${d.recent.length ? html`<div class="tablewrap"><table class="data"><thead><tr><th></th><th>Song</th><th>Artist</th><th>Album</th><th>About</th></tr></thead>
        <tbody>${d.recent.map((r, i) => html`<tr key=${i}><td>${cover(r, 64, "thumb")}</td><td>${r.url ? html`<a href=${r.url} target="_blank" rel="noopener">${r.name}</a>` : r.name}</td>
          <td>${r.artist}</td><td>${r.album}</td><td title=${r.window_start ? `between ${when(r.window_start)} and ${when(r.seen_at)}` : "exact, from Apple's export"}>${when(r.played_at)}</td></tr>`)}</tbody></table></div>`
        : html`<p class="muted">Nothing yet. Plays count from the first read on.</p>`}</div>
    <p class="muted small">Read every ${s.every_minutes} minutes${s.last_ok ? `, last ${ago(s.last_ok)}` : ""}. Apple gives no play times, so days and hours are
      estimates, and it can leave out repeats and some library or playlist plays.${s.imported.plays ? ` Plays up to ${when(s.imported.until)} are from Apple's export, with exact times and what was really heard.` : ""}${s.missed ? ` ${num(s.missed)} reads had more new plays than Apple's list holds, so some were lost.` : ""}</p>`;
}
