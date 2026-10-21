// The Queue page: what the background worker is doing. One worker runs every job in order
// (so the rate limit holds): Flickr jobs (approvals, syncs) and vision jobs (processing:
// EXIF from Flickr, then the vision model). Reached from the reviewer's busy badge.
import { html, useState, useEffect, num, api, ago } from "./lib.js";

const KIND = { apply: "Apply approved changes", process: "Process (EXIF + vision)", sync: "Sync", wx: "WeatherFlow sync" };
const secs = (s) => (s < 60 ? `${s.toFixed(s < 10 ? 1 : 0)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`);

function Job({ j, running }) {
  return html`<div class=${"qjob" + (running ? " running" : "")}>
    ${j.thumb ? html`<img src=${j.thumb} alt="" loading="lazy" />` : html`<span class="qicon">${j.kind === "sync" || j.kind === "wx" ? "⟳" : "·"}</span>`}
    <div class="qwhat"><b>${j.title || "Untitled"}</b>
      <span class="muted">${KIND[j.kind] || j.kind}${j.kind === "sync" && j.mode && j.mode !== "due" ? ` (${j.mode})` : ""}
        ${j.photo_id ? ` · ${j.photo_id}` : ""}</span></div>
    <span class="qpos">${running ? html`<span class="spin">⟳</span> ${secs(Date.now() / 1000 - j.started)}` : `#${j.position}`}</span>
  </div>`;
}

function Lane({ title, about, jobs, stats }) {
  return html`<div class="card qlane"><h2>${title} <span class="badge">${num(jobs.length)}</span></h2>
    <p class="why">${about}</p>
    <div class="kpis small">${stats.map(([k, v]) => html`<div class="kpi"><span>${k}</span><b>${v}</b></div>`)}</div>
    ${jobs.length ? jobs.map((j) => html`<${Job} key=${j.kind + j.position} j=${j} />`)
      : html`<p class="muted">Nothing waiting.</p>`}
  </div>`;
}

export function QueuePage() {
  const [q, setQ] = useState(null);
  const [, tick] = useState(0);
  useEffect(() => {
    let live = true;
    const load = () => api("/api/review/worker").then((d) => live && setQ(d)).catch(() => {});
    load();
    const t = setInterval(() => { load(); tick((n) => n + 1); }, 1500);
    return () => { live = false; clearInterval(t); };
  }, []);
  if (!q) return html`<div class="boot">Loading…</div>`;
  const f = q.flickr, v = q.vision, w = q.weather;
  return html`
    <h1 class="title">Queue</h1>
    <p class="muted">One background worker does these in order, one at a time, so Flickr's rate limit holds.
      ${q.waiting ? ` ${num(q.waiting)} job${q.waiting === 1 ? "" : "s"} waiting.` : " Nothing waiting."}</p>
    <div class="card"><h2>Now</h2>
      ${q.running ? html`<${Job} j=${q.running} running=${true} />` : html`<p class="muted">Idle.</p>`}</div>
    <div class="qlanes">
      <${Lane} title="Flickr API" jobs=${f.jobs}
        about="Approved changes to write, and syncs. Each change re-reads the photo first, so edits you made on Flickr aren't overwritten."
        stats=${[["calls since start", num(f.calls)], ["limit", `${f.calls_per_second}/s`]]} />
      <${Lane} title="Vision model" jobs=${v.jobs}
        about="Photos to process: their EXIF from Flickr, then one model call for categories, tags, title and description."
        stats=${[["model", v.model || "–"], ["photos since start", num(v.photos)],
                 ["tokens in / out", `${num(v.input_tokens)} / ${num(v.output_tokens)}`]]} />
      ${w.configured && html`<${Lane} title="WeatherFlow API" jobs=${w.jobs}
        about="History for your Tempest stations, a day per request, and a gap-fill whenever the live stream connects. The live stream itself isn't a job."
        stats=${[["calls since start", num(w.calls)], ["stream", w.stream ? w.stream.state : "–"],
                 ["messages", num(w.stream?.messages || 0)], ["last message", w.stream?.last_message ? ago(w.stream.last_message) : "–"]]} />`}
    </div>
    ${w.configured && w.requests.length > 0 && html`<div class="card"><h2>Recent WeatherFlow requests</h2>
      <table class="qdone"><thead><tr><th>Request</th><th>Status</th><th>Rows</th><th>Took</th><th>When</th></tr></thead><tbody>
        ${w.requests.slice(0, 15).map((r, i) => html`<tr key=${i}><td class="muted">${r.path}${r.params.time_start ? ` · ${new Date(r.params.time_start * 1000).toLocaleDateString()}` : ""}</td>
          <td><span class=${"status " + (r.status === 200 ? "s-applied" : "s-failed")}>${r.status ?? "…"}</span></td>
          <td>${r.rows ?? ""}</td><td>${r.ms != null ? r.ms + " ms" : ""}</td><td class="muted">${ago(r.at)}</td></tr>`)}
      </tbody></table></div>`}
    <div class="card"><h2>Finished</h2>
      ${q.recent.length ? html`<table class="qdone"><thead><tr><th>Job</th><th>Photo</th><th>Result</th><th>Took</th><th>When</th></tr></thead><tbody>
        ${q.recent.map((r) => html`<tr key=${r.seq}>
          <td>${KIND[r.kind] || r.kind}</td>
          <td>${r.kind === "sync" || r.kind === "wx" ? "" : r.title}</td>
          <td><span class=${"status s-" + r.status}>${r.status}</span>
            ${r.kind === "apply" && (r.applied || r.failed) ? html` <span class="muted">${r.applied} applied${r.failed ? `, ${r.failed} failed` : ""}</span>` : ""}
            ${r.kind === "wx" && r.rows != null ? html` <span class="muted">${num(r.rows)} rows, ${r.calls} calls</span>` : ""}
            ${r.input_tokens ? html` <span class="muted">${num(r.input_tokens + r.output_tokens)} tokens</span>` : ""}
            ${r.error && html`<div class="problem">${r.error}</div>`}</td>
          <td>${r.seconds != null ? secs(r.seconds) : ""}</td>
          <td class="muted">${r.started ? ago(Math.round(r.started)) : ""}</td></tr>`)}
      </tbody></table>` : html`<p class="muted">Nothing since the app started.</p>`}</div>`;
}
