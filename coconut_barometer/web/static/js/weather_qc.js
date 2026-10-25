// Weather → QC (#105): each station's checks against independent data (weather/qc/), signed in only. A card per
// station with a row per check: its status, the numbers that sum it up (in the viewer's units), and since when it's
// said so; then how the reference sampling is going (RTMA, radar, GLM, NWS alerts, fronts) and the past being filled in.
import { html, useState, useEffect, api, ago, num, toast, every } from "./lib.js";

const LEVEL = { pass: "ok", warn: "warn", fail: "bad", insufficient: "off" };
const SAID = { pass: "Pass", warn: "Warn", fail: "Fail", insufficient: "Not enough data" };
const SOURCES = [["rtma", "RTMA analysis"], ["radar", "Radar (MRMS)"], ["glm", "Lightning (GLM)"], ["alerts", "NWS alerts"],
  ["fronts", "Fronts (WPC)"]];
const day = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });

// A check's number in the viewer's units: kind as weather/qc/suites.py has it.
function shown(value, kind, imperial) {
  if (value == null) return "–";
  const signed = (v, d) => `${v > 0 ? "+" : v < 0 ? "−" : "±"}${Math.abs(v).toFixed(d)}`;
  if (kind === "tempd") return imperial ? `${signed(value * 1.8, 1)} °F` : `${signed(value, 1)} °C`;
  if (kind === "tempspread") return imperial ? `${(value * 1.8).toFixed(1)} °F` : `${value.toFixed(1)} °C`;
  if (kind === "temp") return imperial ? `${(value * 1.8 + 32).toFixed(1)} °F` : `${value.toFixed(1)} °C`;
  if (kind === "hpa") return imperial ? `${signed(value * 0.02953, 2)} inHg` : `${signed(value, 1)} hPa`;
  if (kind === "ms") return imperial ? `${(value * 2.23694).toFixed(1)} mph` : `${value.toFixed(1)} m/s`;
  if (kind === "m") return imperial ? `${signed(value * 3.28084, 0)} ft` : `${signed(value, 0)} m`;
  if (kind === "deg") return `${signed(value, 0)}°`;
  if (kind === "pct") return `${Math.round(value)}%`;
  if (kind === "ratio") return `×${value.toFixed(2)}`;
  if (kind === "number") return value.toFixed(2);
  if (kind === "signed") return signed(value, 3);
  return num(value);
}

const when = (ts) => new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const span = (f) => (f.end - f.start < 60 ? when(f.start) : `${when(f.start)} – ${when(f.end)}`);

function Check({ title, run, flags, imperial }) {
  if (!run) return html`<li class="health-row off"><i class="health-dot off"></i><b>${title}</b><span class="muted">not run yet</span></li>`;
  const facts = (run.metrics.show || []).map(([label, v, kind]) => `${shown(v, kind, imperial)} ${label}`).join(" · ");
  return html`<li class=${"health-row " + LEVEL[run.status]}>
    <i class=${"health-dot " + LEVEL[run.status]} aria-label=${SAID[run.status]}></i><b>${title}</b>
    <span>${SAID[run.status]}${facts ? ` · ${facts}` : ""} <span class="muted">· since ${day(run.since)}</span>
      ${flags?.count > 0 && html`<span class="qcflags">${flags.newest.map((f) => html`<small key=${f.start}>${f.what}: ${span(f)}</small>`)}
        ${flags.count > flags.newest.length && html`<small class="muted">and ${flags.count - flags.newest.length} more</small>`}</span>`}</span></li>`;
}

export function StationQc() {
  const [d, setD] = useState(null);
  const [running, setRunning] = useState(false);
  const load = () => api("/api/wx/qc").then(setD).catch((e) => toast(e.message));
  useEffect(() => every(load, 60000), []);
  const runNow = async () => {
    setRunning(true);
    try { await api("/api/wx/qc/run", { method: "POST" }); await load(); }
    catch (e) { toast(e.message); }
    finally { setRunning(false); }
  };
  if (!d) return html`<div class="boot">Loading…</div>`;
  const imperial = d.units === "imperial", now = Date.now() / 1000;
  const fill = d.backfill;
  return html`
    <h1 class="title">Station QC</h1>
    <p class="muted">Each station checked against independent data around it over the last ${d.window_days} days. Checks run
      every hour.${!d.enabled ? " Turned off in Settings → Weather → Station QC." : ""}
      ${d.local ? " Local mode: nothing is sampled here (the server does it); what's shown is whatever this copy of the database has." : ""}</p>
    <div class="toolbar"><button onClick=${runNow} disabled=${running}>${running ? "Running…" : "Run checks now"}</button></div>
    ${!d.stations.length && html`<p class="muted">No stations with a location yet.</p>`}
    ${d.stations.map((s) => html`<section class="card" key=${s.device_id}><h2>${s.name}</h2>
      <ul class="health-rows">${d.suites.map((c) => html`<${Check} key=${c.id} title=${c.title} run=${s.checks[c.id]} flags=${s.flags?.[c.id]} imperial=${imperial} />`)}</ul>
    </section>`)}
    <section class="card"><h2>Reference samples</h2>
      <ul class="health-rows">${SOURCES.map(([k, label]) => {
        const x = d.sampled[k] || {}, err = d.errors[k];
        return html`<li key=${k} class=${"health-row " + (err ? "warn" : x.count ? "ok" : "off")}>
          <i class=${"health-dot " + (err ? "warn" : x.count ? "ok" : "off")}></i><b>${label}</b>
          <span>${x.count ? `${num(x.count)} kept, newest ${ago(x.newest)}, since ${day(x.oldest)}` : "none yet"}${err ? html` · <span class="problem">${err}</span>` : ""}</span></li>`;
      })}</ul>
      <p class="muted">${fill?.to && !fill.done ? `Filling in the past from NOAA's archive (RTMA, hourly): at ${day(fill.to)}, going back ${d.backfill_days} days.`
        : fill?.done && d.backfill_days ? `The last ${d.backfill_days} days are filled in from NOAA's archive.` : ""}
        ${d.errors.checks ? html` <span class="problem">Checks: ${d.errors.checks}</span>` : ""}</p>
    </section>`;
}
