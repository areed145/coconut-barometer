// The Alerts page (#168): the alerts sent, newest first, one row each however many devices it went to, with how it
// went to each on the same line. Details opens the alert's own page (#73: what it said, its evidence, and the chart or
// map from then); the last link opens the chart or map it was about. Settings → Alerts keeps the thresholds and devices.
import { html, useState, useEffect, api, toast } from "./lib.js";

const KINDS = [["", "All alerts"], ["lightning", "Lightning"], ["gust", "Gusts"], ["pressure", "Pressure"], ["storm", "Storms"],
  ["rain", "Rain"], ["callsign", "Callsign heard"], ["place", "Places"], ["altitude", "Altitude"], ["igate", "Igate"],
  ["record", "Igate records"]];
const FAILED = ["failed", "removed"];           // didn't arrive (removed: the subscription had expired)
const MARK = { sent: "✓", failed: "✗", removed: "✗", not_sent: "–" };
const when = (ts) => new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
const short = (s) => (s.length > 40 ? s.slice(0, 39) + "…" : s);

// Every device on one line with how it went, e.g. "iPhone ✓ · iPad ✗ (410 gone)"; the full reason on hover.
function Devices({ deliveries }) {
  const held = deliveries.find((d) => d.status === "suppressed");
  if (held) return html`<span class="muted" title=${held.detail}>held back: ${short(held.detail || "")}</span>`;
  return deliveries.map((d, i) => html`${i ? " · " : ""}<span class=${FAILED.includes(d.status) ? "problem" : ""}
    title=${[d.status.replace("_", " "), d.detail, d.username].filter(Boolean).join(" · ")}>${d.device_label} ${MARK[d.status] ?? d.status}${
    d.detail && d.status !== "sent" ? ` (${short(d.detail)})` : ""}</span>`);
}

export function AlertsLog() {
  const [f, setF] = useState({ kind: "", failed: false });
  const [list, setList] = useState(null);
  const [next, setNext] = useState(null);
  const load = (before) => api("/api/push/history", { params: { kind: f.kind, failed: f.failed || "", before: before || "" } })
    .then((r) => { setList((old) => (before ? [...old, ...r.alerts] : r.alerts)); setNext(r.next); })
    .catch((e) => toast("Couldn't load the alerts: " + e.message));
  useEffect(() => { setList(null); load(); }, [f.kind, f.failed]);
  return html`
    <h1 class="title">Alerts</h1>
    <p class="muted">Every alert sent, and how it went to each device. <a href="#/settings/alerts">Thresholds and devices →</a></p>
    <div class="toolbar">
      <select value=${f.kind} onChange=${(e) => setF({ ...f, kind: e.target.value })} aria-label="Alert type">
        ${KINDS.map(([k, label]) => html`<option value=${k}>${label}</option>`)}</select>
      <button class=${f.failed ? "on" : ""} aria-pressed=${f.failed} onClick=${() => setF({ ...f, failed: !f.failed })}>Failed only</button>
    </div>
    ${!list ? html`<div class="boot">Loading…</div>`
      : !list.length ? html`<p class="muted">${f.kind || f.failed ? "No alerts like that in the log." : "No alerts have been sent yet."}</p>`
      : html`<div class="tablewrap"><table class="data alerts-log"><thead><tr><th>When</th><th>Alert</th><th>Station / callsign</th>
          <th>Evidence</th><th>Devices</th><th></th></tr></thead><tbody>
        ${list.map((x) => html`<tr key=${`${x.at}-${x.key}`}>
          <td class="nowrap">${when(x.at)}</td><td><b>${x.title}</b></td><td>${x.subject}</td>
          <td class="muted">${x.body}</td><td><${Devices} deliveries=${x.deliveries} /></td>
          <td class="nowrap"><a href=${x.url}>Details</a>${x.evidence.link && html` · <a href=${x.evidence.link}>${
            ["storm", "rain"].includes(x.kind) ? "Map" : ["gust", "pressure", "lightning"].includes(x.kind) ? "Chart" : "Open"}</a>`}</td>
        </tr>`)}</tbody></table></div>
        ${next && html`<p><button onClick=${() => load(next)}>Older alerts</button></p>`}`}`;
}
