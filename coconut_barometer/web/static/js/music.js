// Music (#/music): Apple Music listening, read by the server every hour (apple_music.py). Days and hours are in this
// browser's timezone; the times are estimates, since Apple doesn't say when a track was played.
import { html, useState, useEffect, useMemo, api, num, ago } from "./lib.js";
import { Chart } from "./chart.js";
import { Kpi, Bars, Heatmap, Range } from "./viz.js";

const ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const HOURS = [...Array(24).keys()].map(String);
const hours = (minutes) => (minutes >= 600 ? Math.round(minutes / 60) : Math.round(minutes / 6) / 10);
const when = (ts) => new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

function Top({ title, rows }) {
  return html`<div class="card"><h2>${title}</h2><${Bars} rows=${rows} value="plays" empty="Nothing yet." /></div>`;
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
  useEffect(() => { setD(null); api("/api/music/analytics", { params: { days, tz: ZONE } }).then(setD).catch((e) => setD({ error: e.message })); }, [days]);
  const range = html`<${Range} days=${days} setDays=${setDays} />`;
  const series = useMemo(() => (d?.series ? [{ label: "plays", values: d.series.map((r) => r.plays), color: "--chart-1", bars: true }] : []), [d]);
  if (!d) return html`<div class="toolbar">${range}</div><div class="boot">Loading…</div>`;
  if (d.error) return html`<div class="toolbar">${range}</div><p class="problem">${d.error}</p>`;
  const t = d.totals, p = d.previous, s = d.state;
  return html`<div class="toolbar">${range}</div>
    <${Connection} s=${s} empty=${!t.plays && !s.imported.plays} />
    <div class="kpis">
      <${Kpi} label="Plays" now=${t.plays} before=${p?.plays} />
      <${Kpi} label="Hours" now=${hours(t.minutes)} before=${p && hours(p.minutes)} note=${s.imported.plays ? "as heard, in the export" : "if each played to the end"} />
      <${Kpi} label="Songs" now=${t.tracks} before=${p?.tracks} />
      <${Kpi} label="Artists" now=${t.artists} before=${p?.artists} />
    </div>
    <div class="card"><h2>Plays per day</h2><${Chart} days=${d.series.map((r) => r.day)} series=${series} /></div>
    <div class="cols"><${Top} title="Top artists" rows=${d.artists} /><${Top} title="Top songs" rows=${d.tracks} /></div>
    <div class="cols"><${Top} title="Top albums" rows=${d.albums} /><${Top} title="Genres" rows=${d.genres} /></div>
    <div class="card"><h2>When you listen</h2><${Heatmap} cells=${d.heat} rows=${WEEK} cols=${HOURS} unit="plays" /></div>
    <div class="card"><h2>Latest plays</h2>
      ${d.recent.length ? html`<div class="tablewrap"><table class="data"><thead><tr><th>Song</th><th>Artist</th><th>Album</th><th>About</th></tr></thead>
        <tbody>${d.recent.map((r, i) => html`<tr key=${i}><td>${r.url ? html`<a href=${r.url} target="_blank" rel="noopener">${r.name}</a>` : r.name}</td>
          <td>${r.artist}</td><td>${r.album}</td><td title=${r.window_start ? `between ${when(r.window_start)} and ${when(r.seen_at)}` : "exact, from Apple's export"}>${when(r.played_at)}</td></tr>`)}</tbody></table></div>`
        : html`<p class="muted">Nothing yet. Plays count from the first read on.</p>`}</div>
    <p class="muted small">Read every ${s.every_minutes} minutes${s.last_ok ? `, last ${ago(s.last_ok)}` : ""}. Apple gives no play times, so days and hours are
      estimates, and it can leave out repeats and some library or playlist plays.${s.imported.plays ? ` Plays up to ${when(s.imported.until)} are from Apple's export, with exact times and what was really heard.` : ""}${s.missed ? ` ${num(s.missed)} reads had more new plays than Apple's list holds, so some were lost.` : ""}</p>`;
}
