// The Queue page: everything the app asks of the outside world. Top: what's waiting. Each service's limit and
// use, the job running now, and the line of jobs behind it, which one worker takes in order. Inside a job each
// call still waits for its service's rate limit, and those waits show on the service cards. Below: the activity
// log, every call, stream message and finished job as it happens, filterable by source.
import { html, useState, useEffect, useRef, num, api, every } from "./lib.js";

const SOURCES = {
  flickr: ["Flickr API", "--chart-1"], cdn: ["Photo downloads", "--chart-3"], vision: ["Vision model", "--chart-2"],
  weatherflow: ["WeatherFlow REST", "--accent"], "wx-stream": ["WeatherFlow stream", "--warn"], aprs: ["APRS-IS", "--ok"],
  imagery: ["Radar & satellite", "--muted"], geocode: ["Place search", "--muted"], jobs: ["Jobs", "--fg"],
};
const KIND = { apply: "Apply changes", process: "Process photo", undo: "Undo an apply", sync: "Flickr sync",
               wx: "WeatherFlow sync", replan: "Re-plan review" };
const SERVICE = { apply: "flickr", undo: "flickr", sync: "flickr", process: "vision", wx: "weatherflow", replan: "jobs" };
const label = (s) => (SOURCES[s] || [s])[0];
const dot = (s) => html`<i class="srcdot" style=${`background: var(${(SOURCES[s] || [, "--muted"])[1]})`}></i>`;
const dur = (s) => (s == null ? "" : s < 1 ? `${Math.round(s * 1000)} ms` : s < 60 ? `${s.toFixed(s < 10 ? 1 : 0)} s`
  : s < 3600 ? `${Math.floor(s / 60)} min ${Math.round(s % 60)} s` : `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`);
const soon = (s) => (s == null ? "" : s < 5 ? "next" : `in ~${dur(s).replace(/ \d+ s$/, "")}`);
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" });
const ago = (now, ts) => (ts ? `${dur(Math.max(0, now - ts)).replace(/ \d+ s$/, "")} ago` : "never");
const KEEP = 100;      // the most the activity log shows

// ---- what's waiting ----------------------------------------------------------------------
function Service({ id, title, a, on, onPick, children, limit }) {
  const s = a.sources[id] || { calls: 0, errors: 0, last_minute: 0, last_hour: 0, waited: 0 };
  const pending = a.pending.filter((p) => p.source === id);
  const waiting = pending.filter((p) => p.state === "waiting"), running = pending.filter((p) => p.state === "running");
  const budget = limit ? limit * 3600 : null;
  return html`<button class=${"svc" + (on ? " on" : "") + (pending.length ? " busy" : "")} onClick=${() => onPick(on ? "" : id)}
      aria-pressed=${on} title=${on ? "Show every source" : `Show only ${title} in the log`}>
    <span class="svchead">${dot(id)}<b>${title}</b>${s.errors ? html`<span class="status s-failed">${s.errors} error${s.errors === 1 ? "" : "s"}</span>` : ""}</span>
    <span class="svcnum"><b>${num(s.last_hour)}</b> <small>calls in the last hour</small></span>
    <span class="svcmeta">${num(s.last_minute)} in the last minute${s.waited >= 1 ? ` · ${dur(s.waited)} spent waiting for the limit` : ""}</span>
    ${budget && html`<span class="svcbar" title=${`${num(s.last_hour)} of ${num(Math.round(budget))} calls an hour at ${limit}/s`}>
      <i style=${`width: ${Math.min(100, (100 * s.last_hour) / budget)}%`}></i></span>`}
    ${children}
    <span class="svcnow">${running.length ? html`<span class="spin">⟳</span> ${running[0].what}${running.length > 1 ? ` +${running.length - 1}` : ""} · ${dur(a.now - running[0].at)}`
      : waiting.length ? "" : html`<span class="muted">idle · last ${ago(a.now, s.last_at)}</span>`}
      ${waiting.length ? html`<span class="svcwait">${waiting.length} waiting for a slot · next in ${dur(Math.max(0, Math.min(...waiting.map((p) => p.until)) - a.now))}</span>` : ""}</span>
  </button>`;
}

function Stream({ id, title, st, a, on, onPick, extra }) {
  const s = a.sources[id] || { last_hour: 0, skipped: 0, errors: 0 };
  const ok = st?.state === "connected";
  return html`<button class=${"svc" + (on ? " on" : "")} onClick=${() => onPick(on ? "" : id)} aria-pressed=${on}>
    <span class="svchead">${dot(id)}<b>${title}</b><span class=${"status " + (ok ? "s-applied" : "s-failed")} title=${st?.error || ""}>${st ? st.state : "off"}</span></span>
    <span class="svcnum"><b>${num(s.last_hour)}</b> <small>messages in the last hour</small></span>
    <span class="svcmeta">${s.skipped ? `plus ${num(s.skipped)} ${extra}, counted but not listed` : " "}</span>
    <span class="svcnow muted">${st?.error && !ok ? st.error : st?.last_message ? `last message ${ago(a.now, st.last_message)}` : "no messages yet"}</span>
  </button>`;
}

function JobRow({ j, now, running }) {
  return html`<li class=${"qjob" + (running ? " running" : "")}>
    <span class="qpos">${running ? html`<span class="spin">⟳</span>` : j.position}</span>
    ${j.thumb ? html`<img src=${j.thumb} alt="" loading="lazy" />` : html`<span class="qicon">${dot(SERVICE[j.kind])}</span>`}
    <span class="qwhat"><b>${j.title || "Untitled"}</b>
      <span class="muted">${KIND[j.kind] || j.kind}${j.kind === "sync" && j.mode && j.mode !== "due" ? ` (${j.mode})` : ""}
        · ${label(SERVICE[j.kind])}</span></span>
    <span class="qwhen">${running ? html`${dur(now - j.started)}${j.typical ? html`<small>usually ${dur(j.typical)}</small>` : ""}`
      : j.starts_in != null ? soon(j.starts_in) : html`<span class="muted">–</span>`}</span>
  </li>`;
}

function Line({ q, now }) {
  const [all, setAll] = useState(false);
  const shown = all ? q.line : q.line.slice(0, 12);
  return html`<div class="card qline">
    <h2>Up next <span class="badge">${num(q.waiting)}</span>
      ${q.done_in != null && q.waiting ? html`<span class="muted">all done ${soon(q.done_in)}</span>` : ""}</h2>
    ${Object.keys(q.by_kind).length > 0 && html`<p class="qkinds">${Object.entries(q.by_kind).map(([k, n]) => html`<span key=${k}>
      ${dot(SERVICE[k])}${num(n)} ${(KIND[k] || k).toLowerCase()}${q.took[k] ? html` <small>~${dur(q.took[k])} each</small>` : ""}</span>`)}</p>`}
    <ol class="qjobs">
      ${q.running ? html`<${JobRow} j=${q.running} now=${now} running=${true} />` : html`<li class="qjob idle muted">The worker is idle.</li>`}
      ${shown.map((j) => html`<${JobRow} key=${j.kind + j.position} j=${j} now=${now} />`)}
    </ol>
    ${q.line.length > shown.length || all ? html`<button class="link" onClick=${() => setAll(!all)}>
      ${all ? "Show fewer" : `Show all ${num(q.line.length)}${q.waiting > q.line.length ? ` (of ${num(q.waiting)})` : ""}`}</button>` : ""}
    <p class="why">One worker runs these in order, one at a time. Start times come from how long recent jobs of the same
      kind took, including their waits for the rate limit; none are shown until a job of a kind has finished.</p>
  </div>`;
}

// ---- the log ----------------------------------------------------------------------------
function Entry({ e }) {
  const slow = e.waited > 0.05;
  return html`<li class=${"act" + (e.error ? " bad" : "") + (e.kind === "job" ? " job" : "")}>
    <time>${clock(e.at)}</time>
    <span class="actsrc">${dot(e.source)}${label(e.source)}</span>
    <span class="actwhat"><b>${e.what}</b>${e.detail ? html` <span class="muted" title=${e.detail}>${e.detail}</span>` : ""}</span>
    <span class=${"status " + (e.error ? "s-failed" : e.status === "ok" || e.status === "200" || e.status === "done" || e.status === "stored" ? "s-applied" : "s-none")}>${e.status}</span>
    <span class="acttime">${e.ms != null ? dur(e.ms / 1000) : ""}${slow ? html`<small>waited ${dur(e.waited)}</small>` : ""}</span>
  </li>`;
}

function Log({ entries, a, source, setSource, errorsOnly, setErrorsOnly, paused, setPaused, held }) {
  const shown = entries.filter((e) => (!source || e.source === source) && (!errorsOnly || e.error));
  const present = Object.keys(SOURCES).filter((s) => a.sources[s]);
  return html`<div class="card actlog">
    <h2>Activity <span class="muted">the newest ${KEEP}</span></h2>
    <div class="toolbar actbar">
      <span class="seg" aria-label="Source">
        <button class=${!source ? "primary" : ""} onClick=${() => setSource("")}>All</button>
        ${present.map((s) => html`<button key=${s} class=${source === s ? "primary" : ""} onClick=${() => setSource(s)}>${dot(s)}${label(s)}</button>`)}
      </span>
      <button class=${errorsOnly ? "primary" : ""} onClick=${() => setErrorsOnly(!errorsOnly)}>Errors only</button>
      <button onClick=${() => setPaused(!paused)}>${paused ? `Resume${held ? ` (${num(held)} new)` : ""}` : "Pause"}</button>
    </div>
    ${shown.length ? html`<ol class="acts">${shown.map((e) => html`<${Entry} key=${e.seq} e=${e} />`)}</ol>`
      : html`<p class="muted">${entries.length ? "Nothing matches." : "Nothing yet since the app started."}</p>`}
  </div>`;
}

export function QueuePage() {
  const [q, setQ] = useState(null);
  const [a, setA] = useState(null);
  const [entries, setEntries] = useState([]);
  const [source, setSource] = useState("");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [paused, setPaused] = useState(false);
  const [held, setHeld] = useState(0);
  const seq = useRef(0), pausedRef = useRef(false);
  pausedRef.current = paused;
  useEffect(() => {
    let live = true;
    const load = () => {
      api("/api/review/worker").then((d) => live && setQ(d)).catch(() => {});
      api("/api/activity", { params: { since: seq.current, limit: KEEP } }).then((d) => {
        if (!live) return;
        setA(d);
        if (pausedRef.current) { setHeld(d.entries.length); return; }    // read on from the same place when resumed
        seq.current = d.seq;
        setHeld(0);
        if (d.entries.length) setEntries((old) => [...d.entries.reverse(), ...old].slice(0, KEEP));
      }).catch(() => {});
    };
    const stop = every(load, 1500);
    return () => { live = false; stop(); };
  }, []);
  if (!q || !a) return html`<div class="boot">Loading…</div>`;
  const sv = q.services, pick = (s) => setSource(s), on = (s) => source === s;
  const fl = sv.flickr, slot = fl.next_slot && fl.next_slot - a.now > 0.05 ? fl.next_slot - a.now : 0;
  const has = (s) => a.sources[s];
  return html`
    <h1 class="title">Queue</h1>
    <p class="muted">Everything the app asks of other services. Jobs wait in one line; inside a job, each call waits its turn under that
      service's rate limit. Pick a service to see only its activity below.</p>
    <div class="svcs">
      <${Service} id="flickr" title="Flickr API" a=${a} on=${on("flickr")} onPick=${pick} limit=${fl.calls_per_second}>
        <span class="svcmeta">${fl.calls_per_second}/s, shared with every process on this key${slot ? ` · next free slot in ${dur(slot)}` : ""}</span>
      <//>
      <${Service} id="vision" title="Vision model" a=${a} on=${on("vision")} onPick=${pick}>
        <span class="svcmeta">${sv.vision.model || "–"} · ${sv.vision.concurrency} at once · ${num(sv.vision.input_tokens)} in / ${num(sv.vision.output_tokens)} out tokens</span>
      <//>
      ${has("cdn") && html`<${Service} id="cdn" title="Photo downloads" a=${a} on=${on("cdn")} onPick=${pick}>
        <span class="svcmeta">Thumbnails for the vision model, from Flickr's image servers (no API limit)</span><//>`}
      ${sv.weatherflow.configured && html`<${Service} id="weatherflow" title="WeatherFlow REST" a=${a} on=${on("weatherflow")} onPick=${pick} limit=${sv.weatherflow.calls_per_second}>
        <span class="svcmeta">${sv.weatherflow.calls_per_second}/s · a day of history per call</span><//>`}
      ${a.streams["wx-stream"] && html`<${Stream} id="wx-stream" title="WeatherFlow stream" st=${a.streams["wx-stream"]} a=${a} on=${on("wx-stream")} onPick=${pick} extra="wind readings" />`}
      ${a.streams.aprs.callsigns.length > 0 && html`<${Stream} id="aprs" title="APRS-IS" st=${a.streams.aprs} a=${a} on=${on("aprs")} onPick=${pick} extra="keepalives" />`}
      ${has("imagery") && html`<${Service} id="imagery" title="Radar & satellite" a=${a} on=${on("imagery")} onPick=${pick}>
        <span class="svcmeta">Frame lists for the weather map, at most every 4 minutes</span><//>`}
    </div>
    <${Line} q=${q} now=${a.now} />
    <${Log} entries=${entries} a=${a} source=${source} setSource=${setSource} errorsOnly=${errorsOnly} setErrorsOnly=${setErrorsOnly}
      paused=${paused} setPaused=${setPaused} held=${held} />`;
}
