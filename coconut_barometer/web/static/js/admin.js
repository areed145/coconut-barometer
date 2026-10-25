// Settings (layered over config.yaml), API keys, account and sessions.
import { html, useState, useEffect, api, download, upload, toast, ago, until, day, loadTheme, themeChoice, setThemeChoice, every } from "./lib.js";
import { RestoreBackup } from "./setup.js";
import { WxStations } from "./weather.js";

const SOURCE = { default: "default", file: "config.yaml", admin: "set here", env: "environment" };
const lines = (v) => (v || []).join("\n");
const unlines = (s) => s.split("\n").map((x) => x.trim()).filter(Boolean);
const commas = (v) => (Array.isArray(v) ? v.join(", ") : v || "");
const uncommas = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
const NUMERIC = new Set(["lat", "lon", "radius_km"]);

function ThemeSetting() {
  const [choice, setChoice] = useState(themeChoice());
  const change = (e) => { const value = e.target.value; setChoice(value); setThemeChoice(value); };
  return html`<div class="card theme-setting">
    <label for="interface-theme"><b>Interface theme</b></label>
    <select id="interface-theme" value=${choice} onChange=${change}>
      <option value="auto">Auto (follow this device)</option>
      <option value="light">Light</option>
      <option value="dark">Dark</option>
    </select>
  </div>`;
}

function ListField({ value, onChange, disabled }) {
  const [text, setText] = useState(lines(value));
  useEffect(() => setText(lines(value)), [JSON.stringify(value)]);
  return html`<textarea rows=${Math.min(8, Math.max(2, (value || []).length + 1))} disabled=${disabled} value=${text}
    onInput=${(e) => { setText(e.target.value); onChange(unlines(e.target.value)); }} placeholder="one per line"></textarea>`;
}

function MapField({ value, onChange, disabled }) {
  const rows = Object.entries(value || {});
  const set = (next) => onChange(Object.fromEntries(next.filter(([k]) => k.trim())));
  return html`<div class="maprows">
    ${rows.map(([k, v], i) => html`<div class="maprow" key=${i}>
      <input value=${k} disabled=${disabled} aria-label="key"
             onChange=${(e) => set(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} />
      <span class="muted">→</span>
      <input value=${commas(v)} disabled=${disabled} aria-label="value"
             onChange=${(e) => set(rows.map((r, j) => (j === i ? [r[0], uncommas(e.target.value)] : r)))} />
      ${!disabled && html`<button class="link" onClick=${() => set(rows.filter((_, j) => j !== i))} aria-label="Remove">✕</button>`}
    </div>`)}
    ${!disabled && html`<button class="link" onClick=${() => onChange({ ...(value || {}), [`new${rows.length + 1}`]: [] })}>+ Add</button>`}
  </div>`;
}

// Settings → Display → Radar colors (and Lightning colors): a dBZ (minutes ago) and a color per stop, blended in
// between on the Weather map.
function StopsField({ value, onChange, disabled, k = "dbz", unit = "dBZ", top = 90, gap = 10 }) {
  const rows = [...(value || [])].sort((a, b) => a[k] - b[k]);
  const set = (i, patch) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const next = rows.length ? Math.min(top, rows[rows.length - 1][k] + gap) : Math.min(top, 20);
  return html`<div class="maprows">
    ${rows.map((r, i) => html`<div class="maprow" key=${i}>
      <input type="number" min="0" max=${top} step="1" value=${r[k]} disabled=${disabled} aria-label=${unit}
             onChange=${(e) => set(i, { [k]: Number(e.target.value) })} /><span class="muted">${unit}</span>
      <span class="colorfield"><input type="color" value=${r.color} disabled=${disabled} aria-label="color"
             onInput=${(e) => set(i, { color: e.target.value })} /><code>${r.color}</code></span>
      ${!disabled && rows.length > 2 && html`<button class="link" onClick=${() => onChange(rows.filter((_, j) => j !== i))} aria-label="Remove">✕</button>`}
    </div>`)}
    ${!disabled && html`<button class="link" onClick=${() => onChange([...rows, { [k]: next, color: rows[rows.length - 1]?.color || "#ffffff" }])}>+ Add a stop</button>`}
  </div>`;
}

// Settings → APRS → Stations that report: a row per Tempest the site knows, each with the callsign-SSID it
// reports to CWOP as (blank: it doesn't). Saved as {station name: callsign}; a name no station has any more
// (renamed or gone) is listed so it can be removed.
function CwopField({ value, onChange, disabled }) {
  const [stations, setStations] = useState(null);
  useEffect(() => { api("/api/wx/stations").then((d) => setStations(d.stations.map((s) => s.name))).catch(() => setStations([])); }, []);
  const map = Object.fromEntries(Object.entries(value || {}).map(([k, v]) => [k, Array.isArray(v) ? v.join(", ") : v || ""]));
  const set = (name, call) => { const next = { ...map, [name]: call.trim().toUpperCase() }; if (!next[name]) delete next[name]; onChange(next); };
  if (!stations) return html`<p class="muted">Loading stations…</p>`;
  const gone = Object.keys(map).filter((k) => !stations.some((n) => n.toLowerCase() === k.toLowerCase()));
  const callFor = (name) => map[Object.keys(map).find((k) => k.toLowerCase() === name.toLowerCase())] || "";
  return html`<div class="maprows">
    ${!stations.length && html`<p class="muted">No stations yet: they appear after the first WeatherFlow sync.</p>`}
    ${stations.map((name) => html`<div class="maprow" key=${name}>
      <span>${name}</span><span class="muted">→</span>
      <input value=${callFor(name)} disabled=${disabled} placeholder="off" aria-label=${`Callsign for ${name}`}
             onChange=${(e) => set(Object.keys(map).find((k) => k.toLowerCase() === name.toLowerCase()) || name, e.target.value)} />
    </div>`)}
    ${gone.map((name) => html`<div class="maprow" key=${"gone" + name}>
      <span class="muted">${name} (no such station)</span><span class="muted">→</span><span>${map[name]}</span>
      ${!disabled && html`<button class="link" onClick=${() => set(name, "")} aria-label=${`Remove ${name}`}>✕</button>`}
    </div>`)}
  </div>`;
}

function ItemsField({ s, value, onChange, disabled }) {
  const items = value || [];
  const set = (i, field, raw) => {
    const v = NUMERIC.has(field) ? (raw === "" ? undefined : Number(raw)) : field === "tags" ? uncommas(raw) : raw;
    onChange(items.map((it, j) => { if (j !== i) return it; const n = { ...it, [field]: v }; if (v === undefined || v === "") delete n[field]; return n; }));
  };
  return html`<div class="items"><table><thead><tr>${s.fields.map((f) => html`<th>${f}${s.required.includes(f) ? "" : " (optional)"}</th>`)}<th></th></tr></thead>
    <tbody>${items.map((it, i) => html`<tr key=${i}>${s.fields.map((f) => html`<td><input disabled=${disabled}
        type=${NUMERIC.has(f) ? "number" : "text"} step="any" value=${f === "tags" ? commas(it[f]) : it[f] ?? ""}
        onChange=${(e) => set(i, f, e.target.value)} aria-label=${f} /></td>`)}
      <td>${!disabled && html`<button class="link" onClick=${() => onChange(items.filter((_, j) => j !== i))} aria-label="Remove">✕</button>`}</td></tr>`)}
    </tbody></table>
    ${!disabled && html`<button class="link" onClick=${() => onChange([...items, {}])}>+ Add</button>`}</div>`;
}

function CategoriesField({ value, onChange, albums }) {
  const cats = Object.entries(value || {});
  const put = (next) => onChange(Object.fromEntries(next));
  const edit = (i, patch) => put(cats.map(([k, c], j) => (j === i ? [patch.name ?? k, { ...c, ...patch.c }] : [k, c])));
  return html`<datalist id="album-titles">${albums.map((a) => html`<option value=${a} />`)}</datalist>
    <div class="cats">${cats.map(([name, c], i) => html`<div class="card cat" key=${i}>
      <div class="catrow"><label>Name<input value=${name} onChange=${(e) => edit(i, { name: e.target.value.trim() || name })} /></label>
        <label>Album<input list="album-titles" value=${c.album || ""} onChange=${(e) => edit(i, { c: { album: e.target.value } })} /></label>
        <label>Tags<input value=${commas(c.tags)} onChange=${(e) => edit(i, { c: { tags: uncommas(e.target.value) } })} /></label>
        <button class="link danger" onClick=${() => put(cats.filter((_, j) => j !== i))}>Remove</button></div>
      <label>What the model reads<textarea rows="3" value=${c.description || ""}
        onInput=${(e) => edit(i, { c: { description: e.target.value } })}></textarea></label>
    </div>`)}</div>
    <button onClick=${() => put([...cats, [`category${cats.length + 1}`, { album: "", description: "", tags: [] }]])}>+ Add category</button>`;
}

function SecretField({ s, onSaved }) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [password, setPassword] = useState("");
  const send = async (clear) => {
    try {
      const r = await api(`/api/admin/secrets/${s.path}`, { method: "PUT", body: { value: clear ? null : value, password } });
      setOpen(false); setValue(""); setPassword(""); toast(clear ? "Cleared." : "Saved, encrypted."); onSaved(r);
    } catch (e) { toast(e.status === 403 ? "Wrong password." : e.message); }
  };
  return html`<div>
    <span>${s.set ? html`<b>set</b>${s.hint && html` <span class="muted">· ends …${s.hint}</span>`}` : html`<span class="muted">not set</span>`}</span>
    ${!s.locked && !open && html` <button class="link" onClick=${() => setOpen(true)}>${s.set ? "Replace" : "Set"}</button>`}
    ${open && html`<form class="secretform" onSubmit=${(e) => { e.preventDefault(); send(false); }}>
      <input type="password" autocomplete="off" placeholder="New value" value=${value} onInput=${(e) => setValue(e.target.value)} />
      <input type="password" autocomplete="current-password" placeholder="Your password" required value=${password}
             onInput=${(e) => setPassword(e.target.value)} />
      <button class="primary" disabled=${!value}>Save</button>
      ${s.set && s.source === "admin" && html`<button type="button" class="danger" onClick=${() => send(true)} disabled=${!password}>Clear</button>`}
      <button type="button" class="link" onClick=${() => setOpen(false)}>Cancel</button></form>`}
  </div>`;
}

function Field({ s, value, dirty, onChange, onReset, albums, onSaved }) {
  const disabled = !!s.locked;
  let input;
  if (s.path === "theme.radar_colors") input = html`<${StopsField} value=${value} onChange=${onChange} disabled=${disabled} />`;
  else if (s.path === "theme.lightning_colors") input = html`<${StopsField} value=${value} onChange=${onChange} disabled=${disabled}
      k="min" unit="min ago" top=${15} gap=${3} />`;
  else if (s.kind === "secret") input = html`<${SecretField} s=${s} onSaved=${onSaved} />`;
  else if (s.kind === "bool") input = html`<input type="checkbox" checked=${!!value} disabled=${disabled} onChange=${(e) => onChange(e.target.checked)} />`;
  else if (s.kind === "int" || s.kind === "float") input = html`<input type="number" step=${s.kind === "int" ? 1 : "any"} value=${value ?? ""} disabled=${disabled}
      onInput=${(e) => onChange(e.target.value === "" ? null : s.kind === "int" ? parseInt(e.target.value, 10) : parseFloat(e.target.value))} />`;
  else if (s.kind === "choice") input = html`<select value=${value} disabled=${disabled} onChange=${(e) => onChange(e.target.value)}>
      ${s.options.map((o) => html`<option value=${o}>${s.option_labels?.[o] || o}</option>`)}</select>`;
  else if (s.kind === "color") input = html`<span class="colorfield"><input type="color" value=${value} disabled=${disabled}
      onInput=${(e) => onChange(e.target.value)} aria-label=${s.label} /><code>${value}</code></span>`;
  else if (s.kind === "text") input = html`<textarea rows="6" value=${value || ""} disabled=${disabled} placeholder="(the built-in brief)"
      onInput=${(e) => onChange(e.target.value.trim() ? e.target.value : null)}></textarea>`;
  else if (s.kind === "list") input = html`<${ListField} value=${value} onChange=${onChange} disabled=${disabled} />`;
  else if (s.path === "aprs.cwop") input = html`<${CwopField} value=${value} onChange=${onChange} disabled=${disabled} />`;
  else if (s.kind === "map") input = html`<${MapField} value=${value} onChange=${onChange} disabled=${disabled} />`;
  else if (s.kind === "items") input = html`<${ItemsField} s=${s} value=${value} onChange=${onChange} disabled=${disabled} />`;
  else if (s.kind === "categories") input = html`<${CategoriesField} value=${value} onChange=${onChange} albums=${albums} />`;
  else input = html`<input type="text" value=${value ?? ""} disabled=${disabled}
      onInput=${(e) => onChange(e.target.value === "" && s.default === null ? null : e.target.value)} />`;
  const wide = ["text", "list", "map", "items", "categories"].includes(s.kind);
  return html`<div class=${"setting" + (wide ? " wide" : "") + (dirty ? " dirty" : "") + (disabled ? " locked" : "")}>
    <div class="label"><b>${s.label}</b>
      <span class=${"src src-" + s.source}>${SOURCE[s.source]}</span>
      ${s.source === "admin" && s.kind !== "secret" && !dirty && html`<button class="link" onClick=${onReset} title="Use config.yaml / the default again">↺ reset</button>`}
      ${s.help && html`<div class="why">${s.help}</div>`}
      ${s.locked && html`<div class="why">🔒 ${typeof s.locked === "string" && s.locked.startsWith("set by") ? s.locked : `Only in config.yaml or the environment: ${s.locked}.`}</div>`}
    </div>
    <div class="input">${input}</div>
  </div>`;
}

function FlickrPanel() {
  const [s, setS] = useState(null);
  useEffect(() => { api("/api/admin/flickr").then(setS).catch(() => setS({ connected: false })); }, []);
  const connect = async () => {
    try { const { url } = await api("/api/admin/flickr/connect", { method: "POST" }); location.href = url; }
    catch (e) { toast(e.message); }
  };
  if (!s) return null;
  return html`<div class="card"><h2>Flickr account</h2>
    <p>${s.connected ? html`<b class="up">Connected</b>${s.nsid && html` <span class="muted">as ${s.nsid}</span>`}`
      : html`<b class="problem">Not connected</b>`}</p>
    ${!s.keys_set ? html`<p class="why">Set the Flickr API key and secret first.</p>`
      : html`<button class=${s.connected ? "" : "primary"} onClick=${connect}>${s.connected ? "Reconnect" : "Connect Flickr"}</button>
        <p class="why">Opens flickr.com to approve access (read, write, and delete), then brings you back here.</p>`}
  </div>`;
}

function AprsStream() {
  const [d, setD] = useState(null);
  const load = () => api("/api/aprs/stations").then(setD).catch(() => {});
  useEffect(() => every(load, 5000), []);
  if (!d) return null;
  const s = d.stream, c = d.cwop, ok = s.state === "connected";
  return html`<div class="card"><h2>APRS-IS connection</h2>
    <p><span class=${"status " + (ok ? "s-applied" : "s-failed")} title=${s.error || ""}>${ok ? "connected" : s.state}</span>
      <span class="muted"> · ${s.server ? `${s.server} · ` : ""}${s.last_message ? `last packet ${ago(s.last_message)}` : "no packets yet this session"}</span></p>
    ${c && html`<p>Weather reports (CWOP): ${!c.reporting.length ? html`<span class="muted">off: no station has a callsign below</span>`
      : html`${c.reporting.map((r) => `${r.station} as ${r.callsign}`).join(", ")}, every ${c.every_minutes} min ·
        ${c.error ? html`<b class="problem">${c.error}</b>` : c.last_at ? html`<span class="muted">last sent ${ago(c.last_at)} (${c.last_sent.join(", ")})</span>`
          : html`<span class="muted">first report within a few minutes</span>`}`}</p>`}
  </div>`;
}

// ---- Alerts: Web Push to this device (sw.js shows them; alerts.py decides what) -----------------------------
const IOS = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const INSTALLED = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const PUSH = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const keyBytes = (b64) => Uint8Array.from(atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
const deviceName = () => {
  const ua = navigator.userAgent;
  const os = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) || IOS ? "iPad" : /Android/.test(ua) ? "Android" : /Mac/.test(ua) ? "Mac"
    : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "A device";
  const app = INSTALLED ? "app" : /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "";
  return app ? `${os} · ${app}` : os;
};

function AlertsPanel() {
  const [d, setD] = useState(null);
  const [history, setHistory] = useState([]);
  const [mine, setMine] = useState(null);       // this browser's subscription's device id, if it's on
  const [busy, setBusy] = useState(false);
  const loadHistory = () => api("/api/push/history").then((x) => setHistory(x.alerts)).catch(() => {});
  const load = async () => {
    const data = await api("/api/push");
    setD(data);
    if (!PUSH) return;
    const sub = await (await navigator.serviceWorker.ready).pushManager.getSubscription();
    setMine(sub ? await idOf(sub.endpoint, data) : null);
  };
  const idOf = async (endpoint, data) => {     // push.device_id(): the endpoint's sha256, first 16 hex digits
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint))))
      .map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
    return data.devices.some((x) => x.id === hash) ? hash : null;
  };
  useEffect(() => { load().catch((e) => toast(e.message)); }, []);
  useEffect(() => every(loadHistory, 60000), []);
  const on = async () => {
    setBusy(true);
    try {
      if ((await Notification.requestPermission()) !== "granted") throw new Error("Notifications are blocked for this site in the browser's settings.");
      const reg = await navigator.serviceWorker.ready;
      const sub = (await reg.pushManager.getSubscription())
        || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(d.public_key) });
      const dev = await api("/api/push/subscribe", { method: "POST", body: { subscription: sub.toJSON(), label: deviceName() } });
      await api("/api/push/test", { method: "POST", body: { id: dev.id } });
      toast("Alerts on: a test notification is on its way.");
      await load();
    } catch (e) { toast(e.message); }
    setBusy(false);
  };
  const off = async (id) => {
    setBusy(true);
    try {
      if (id === mine) await (await (await navigator.serviceWorker.ready).pushManager.getSubscription())?.unsubscribe();
      await api("/api/push/unsubscribe", { method: "POST", body: { id } });
      await load();
    } catch (e) { toast(e.message); }
    setBusy(false);
  };
  const test = async () => {
    try { const r = await api("/api/push/test", { method: "POST", body: {} }); toast(`Sent to ${r.sent} device${r.sent === 1 ? "" : "s"}` + (r.failed.length ? `; ${r.failed.join("; ")}` : "")); }
    catch (e) { toast(e.message); }
  };
  if (!d) return null;
  return html`<div class="card"><h2>Devices</h2>
    ${!PUSH ? html`<p class="why">${IOS && !INSTALLED
        ? html`On iPhone and iPad, alerts reach the app on the Home Screen, not Safari: tap Share → <b>Add to Home Screen</b>, open it from there, and turn alerts on here.`
        : "This browser can't receive notifications from websites."}</p>`
      : mine ? html`<p><b class="up">On for this device</b> <button class="link" onClick=${() => off(mine)} disabled=${busy}>Turn off</button></p>`
      : html`<p><button class="primary" onClick=${on} disabled=${busy}>${busy ? "Turning on…" : "Turn on alerts for this device"}</button></p>
        <p class="why">The browser asks to allow notifications, then a test one arrives.</p>`}
    ${d.devices.length > 0 && html`
      ${d.devices.map((x) => html`<div class="sessionrow" key=${x.id}>
        <span>${x.label}${x.id === mine ? html` <span class="muted">(this one)</span>` : ""} <span class="muted">· ${x.username} · since ${day(x.added)}</span></span>
        <button class="link" onClick=${() => off(x.id)} disabled=${busy}>Remove</button></div>`)}
      <button onClick=${test}>Send a test to all</button>`}
  </div>
  <div class="card"><h2>Recent notifications</h2>
    <p class="why">Delivery attempts, the measurements or radar details behind each alert, and a link to its chart or map.</p>
    ${!history.length ? html`<p class="muted">No alert notifications have been sent yet.</p>` : html`
      <div class="alert-history">${history.map((x) => html`<article class="alert-log" key=${`${x.at}-${x.device_id}-${x.alert_key}`}>
        <div class="alert-log-head"><b>${x.title}</b><span class=${x.status === "sent" ? "up" : "problem"}>${x.status}</span></div>
        <p>${x.body}</p>
        <p class="muted">${new Date(x.at * 1000).toLocaleString()} · ${x.device_label} · ${x.username}</p>
        ${Object.keys(x.evidence || {}).length > 0 && html`<p class="alert-evidence">${Object.entries(x.evidence).filter(([k]) => k !== "link").map(([k, v]) =>
          html`<span>${k.replaceAll("_", " ")}: ${String(v)}</span>`)}</p>`}
        ${x.detail && html`<p class="problem">${x.detail}</p>`}
        <a href=${x.url}>View chart or map</a>
      </article>`)}</div>`}
  </div>`;
}

function SyncPanel() {
  const [s, setS] = useState(null);
  const load = () => api("/api/sync/status").then(setS).catch(() => {});
  useEffect(() => every(load, 5000), []);
  const run = async (mode) => {           // mode: none = whatever's due; "photo_list" = re-read every photo
    try { const r = await api("/api/sync/run", { method: "POST", body: mode ? { mode } : {} });
      toast(r.status === "busy" ? "A sync is already running." : r.status === "no_flickr"
        ? "Connect Flickr first (API keys)." : "Sync started."); load();
    } catch (e) { toast("Couldn't start a sync: " + e.message); }
  };
  if (!s) return null;
  const err = s.last_result?.error;
  return html`<div class="card"><h2>Sync now</h2>
    <p>${s.running ? html`<a href="#/status"><span class="spin">⟳</span> Syncing…</a>`
      : err ? html`<b class="problem">Last sync failed:</b> ${err}`
      : html`Synced ${ago(s.last_sync)}${s.next_run ? html` <span class="muted">· next ${until(s.next_run)}</span>` : ""}`}</p>
    <div class="actions">
      <button class="primary" onClick=${() => run()} disabled=${s.running}>Sync now</button>
      <button onClick=${() => run("photo_list")} disabled=${s.running}
              title="View counts, the daily snapshot and each photo's location privacy (~22 calls)">Re-read every photo</button>
    </div>
    <p class="why">Sync now does whatever is due. Re-read every photo refreshes view counts, the daily snapshot and
      which locations are public, without waiting for the daily run.</p>
  </div>`;
}

function BackupsPanel() {
  const [b, setB] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = () => api("/api/admin/backups").then(setB);
  useEffect(() => { load(); }, []);
  const now = async () => { setBusy(true); try { const r = await api("/api/admin/backups", { method: "POST" }); toast("Backed up: " + r.name); load(); } catch (e) { toast(e.message); } setBusy(false); };
  const fetchBackup = async (name) => {
    const password = prompt("Your password (a backup is the whole database):");
    if (!password) return;
    try { await download(`/api/admin/backups/${encodeURIComponent(name)}/download`, { password }, name); }
    catch (e) { toast(e.status === 403 ? "Wrong password." : `Couldn't download (${e.message}).`); }
  };
  const fetchKey = async () => {
    const password = prompt("Your password (with a backup, this key reveals your API keys):");
    if (!password) return;
    try { await download("/api/admin/encryption-key/download", { password }, "secret.key"); }
    catch (e) { toast(e.status === 403 ? "Wrong password." : `Couldn't download (${e.message}).`); }
  };
  const restore = (file, key) => {
    const password = prompt(`Replace this whole database with ${file.name}? The app restarts into it. Your password:`);
    return password ? upload("/api/admin/restore", file, { "X-Confirm-Password": password, "X-Encryption-Key": key }) : null;
  };
  if (!b) return null;
  return html`<div class="card"><h2>Backups</h2>
    <p class="muted">${b.every_hours ? `Automatic every ${b.every_hours} h` : "Automatic backups are off"} · last ${ago(b.last_backup)}</p>
    ${b.last_error && html`<p class="problem">Last problem: ${b.last_error}</p>`}
    ${b.backups.map((x) => html`<div class="sessionrow" key=${x.name}><span>${x.name} <span class="muted">· ${(x.size / 1048576).toFixed(1)} MB · ${day(x.created_at)}</span></span>
      <button class="link" onClick=${() => fetchBackup(x.name)}>Download</button></div>`)}
    <button onClick=${now} disabled=${busy}>${busy ? "Backing up…" : "Back up now"}</button>
    <h3>Encryption key</h3>
    <p class="why">Backups leave out the key their API keys are encrypted with. To restore one on another server with its API keys,
      bring <code>secret.key</code> too: drop it in with the backup. Keep it apart from the backups: together they reveal the API keys.</p>
    <button onClick=${fetchKey}>Download encryption key</button>
    <h3>Restore a backup</h3>
    <p class="why">Replaces this database (the current one is kept beside it). Leave the key empty if the backup came from this server.</p>
    <${RestoreBackup} send=${restore} />
  </div>`;
}

function Account() {
  const [sessions, setSessions] = useState([]);
  const [log, setLog] = useState([]);
  const [pw, setPw] = useState({ current: "", new: "", again: "" });
  const load = () => { api("/api/admin/sessions").then(setSessions); api("/api/admin/log").then(setLog); };
  useEffect(load, []);
  const change = async (e) => {
    e.preventDefault();
    if (pw.new !== pw.again) return toast("The new passwords don't match.");
    try { await api("/api/admin/password", { method: "POST", body: { current: pw.current, new: pw.new } });
      toast("Password changed. Sign in again."); window.dispatchEvent(new Event("cb:logged-out"));
    } catch (err) { toast(err.status === 403 ? "Wrong current password." : err.message); }
  };
  const end = async (id) => { await api(`/api/admin/sessions/${id}`, { method: "DELETE" }); load(); };
  return html`<div class="card"><h2>Password</h2>
      <form class="pwform" onSubmit=${change}>
        <input type="password" autocomplete="current-password" placeholder="Current password" required value=${pw.current} onInput=${(e) => setPw({ ...pw, current: e.target.value })} />
        <input type="password" autocomplete="new-password" placeholder="New password (12+ characters)" minlength="12" required value=${pw.new} onInput=${(e) => setPw({ ...pw, new: e.target.value })} />
        <input type="password" autocomplete="new-password" placeholder="New password again" required value=${pw.again} onInput=${(e) => setPw({ ...pw, again: e.target.value })} />
        <button class="primary">Change password</button>
      </form><p class="why">Changing it signs out every session, this one too.</p></div>
    <div class="card"><h2>Signed-in sessions</h2>${sessions.map((s) => html`<div class="sessionrow" key=${s.id}>
      <span><b>${s.current ? "This browser" : s.user_agent.split(") ")[0].replace(/^Mozilla\/5.0 \(/, "") || "Unknown"}</b>
        <span class="muted"> · ${s.ip} · active ${ago(s.last_seen)} · expires ${day(s.expires_at)}</span></span>
      ${!s.current && html`<button class="link danger" onClick=${() => end(s.id)}>Sign out</button>`}</div>`)}</div>
    <div class="card"><h2>Recent changes</h2>${log.length ? log.map((r) => html`<div class="logrow" key=${r.id}>
      <span class="muted">${day(r.at)} · ${r.username}</span> <b>${r.path}</b> <span class="muted">${r.old} → ${r.new}</span></div>`)
      : html`<p class="muted">No changes yet.</p>`}</div>`;
}

// The section is in the address (#/settings/weather), so other pages can link straight to it.
export function Admin({ at }) {
  const [data, setData] = useState(null);
  const [edits, setEdits] = useState({});
  const [errors, setErrors] = useState([]);
  const [saving, setSaving] = useState(false);
  useEffect(() => { api("/api/admin/settings").then(setData).catch((e) => toast(e.message)); }, []);
  if (!data) return html`<div class="boot">Loading…</div>`;
  const loaded = (d) => { setData(d); setErrors([]); };
  const save = async (reset = []) => {
    setSaving(true);
    try { const d = await api("/api/admin/settings", { method: "PUT", body: { changes: edits, reset } });
      loaded(d); setEdits({}); loadTheme(); toast(d.saved.length ? `Saved ${d.saved.length} setting(s). Applied now.` : "Nothing changed.");
    } catch (e) { setErrors(e.status === 422 ? e.message.split("; ") : [e.message]); }
    setSaving(false);
  };
  const section = at === "account" || data.sections.some((s) => s.id === at) ? at : "photos";
  const setSection = (id) => location.replace("#/settings/" + id);
  const dirty = Object.keys(edits).length;
  const shown = data.settings.filter((s) => s.section === section);
  return html`<div class="admin">
    <nav class="sections">${[...data.sections, { id: "account", title: "Account" }].map((sec) => html`<button
        class=${section === sec.id ? "on" : ""} onClick=${() => setSection(sec.id)}>${sec.title}${
        data.settings.some((s) => s.section === sec.id && edits[s.path] !== undefined) ? " •" : ""}</button>`)}</nav>
    <div class="settings">
      ${data.problems.map((p) => html`<p class="problem">⚠ ${p}</p>`)}
      ${section === "account" ? html`<${Account} />` : html`
        <h1 class="title">${data.sections.find((s) => s.id === section)?.title}</h1>
        <p class="muted">${data.sections.find((s) => s.id === section)?.help}</p>
        ${section === "connections" && html`<${FlickrPanel} />`}
        ${section === "display" && html`<${ThemeSetting} />`}
        ${section === "weather" && html`<${WxStations} />`}
        ${section === "aprs" && html`<${AprsStream} />`}
        ${section === "alerts" && html`<${AlertsPanel} />`}
        ${section === "backup" && html`<${BackupsPanel} />`}
        ${shown.map((s, i) => {
          const newPart = s.part && s.part !== shown[i - 1]?.part;
          return html`${newPart && html`<h2 class="part" key=${"p" + s.part}>${s.part}</h2>`}
          ${newPart && s.part === "Sync" && html`<${SyncPanel} />`}
          ${s.group && (newPart || s.group !== shown[i - 1]?.group) && html`<h3 class="group" key=${"g" + s.part + s.group}>${s.group}</h3>`}
          <${Field} key=${s.path} s=${s} albums=${data.albums}
            value=${edits[s.path] !== undefined ? edits[s.path] : s.value} dirty=${edits[s.path] !== undefined}
            onChange=${(v) => setEdits({ ...edits, [s.path]: v })} onReset=${() => save([s.path])} onSaved=${loaded} />`;
        })}`}
    </div>
    ${(dirty > 0 || errors.length > 0) && html`<div class="savebar">
      ${errors.length > 0 && html`<ul class="errors">${errors.map((e) => html`<li>${e}</li>`)}</ul>`}
      <span>${dirty} unsaved change${dirty === 1 ? "" : "s"}</span>
      <button class="primary" onClick=${() => save()} disabled=${saving || !dirty}>${saving ? "Saving…" : "Save"}</button>
      <button onClick=${() => { setEdits({}); setErrors([]); }}>Discard</button>
    </div>`}
  </div>`;
}
