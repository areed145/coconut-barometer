// First run (no login yet): create one, or restore a backup. Also the restore form in Settings.
import { html, useState, useEffect, api, upload, waitForRestart, DropZone, num } from "./lib.js";

const isKeyFile = (f) => /\.key$/i.test(f.name) || (f.size < 200 && !/\.(gz|db)$/i.test(f.name));

// send(file, key) uploads the backup; the server checks it and restarts into it.
export function RestoreBackup({ send, disabled }) {
  const [file, setFile] = useState(null);
  const [key, setKey] = useState("");
  const [state, setState] = useState({ busy: false, error: "", done: null });
  const onFiles = async (files) => {
    for (const f of files) {
      if (isKeyFile(f)) setKey((await f.text()).trim());
      else setFile(f);
    }
  };
  const go = async () => {
    setState({ busy: true, error: "", done: null });
    try {
      const r = await send(file, key);
      if (!r) { setState({ busy: false, error: "", done: null }); return; }
      setState({ busy: true, error: "", done: r });
      waitForRestart();
    } catch (e) {
      setState({ busy: false, error: e.status === 403 ? e.message || "Wrong password." : e.message, done: null });
    }
  };
  const d = state.done;
  if (d) return html`<div class="restore"><p><span class="spin">⟳</span> Uploaded and checked. Restoring and restarting:
      this page reloads by itself when the app is back (usually under a minute).</p>
    ${d.keys > 0 && !d.key_given && !d.readable && html`<p class="problem">This backup's ${d.keys} API keys are encrypted with a key this
      server doesn't have, so you'll need to set them again in Settings.</p>`}
    ${d.key_ignored && html`<p class="problem">FLICKR_ORGANIZER_SECRET_KEY is set on the server and wins over the key you gave:
      delete it in the server's environment.</p>`}</div>`;
  return html`<div class="restore">
    <${DropZone} onFiles=${onFiles} accept=".gz,.db,.key" disabled=${disabled || state.busy}>
      ${file ? html`<strong>${file.name}</strong> <span class="muted">· ${(file.size / 1048576).toFixed(1)} MB · drop another to replace</span>`
        : html`<strong>Drop a backup here</strong> <span class="muted">(.db.gz from <code>flickr-organizer backup</code>), or click to choose.
          Drop <code>secret.key</code> too to bring the API keys.</span>`}
    <//>
    <label for="restore-key">Encryption key <span class="muted">(optional)</span></label>
    <input id="restore-key" type="password" autocomplete="off" spellcheck="false" value=${key} disabled=${state.busy}
           onInput=${(e) => setKey(e.target.value)} placeholder="contents of ~/.config/flickr-organizer/secret.key" />
    <p class="why">The key the backup's API keys are encrypted with, from the computer it came from. Without it they can't be read,
      and you set them again in Settings.</p>
    <button class="primary" disabled=${!file || disabled || state.busy} onClick=${go}>${state.busy ? "Uploading…" : "Restore"}</button>
    ${state.error && html`<p class="error" role="alert">${state.error}</p>`}
  </div>`;
}

// Import the former SQLite library: start it with the setup code, follow /api/migration/status, then sign in as before.
function ImportSqlite({ code }) {
  const [m, setM] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const load = () => api("/api/migration/status").then(setM).catch(() => {});
    load();
    const t = setInterval(load, m && m.state === "running" ? 1000 : 5000);
    return () => clearInterval(t);
  }, [m && m.state]);
  const start = async () => {
    setError("");
    try { setM(await api("/api/setup/migrate", { method: "POST", body: { code } })); }
    catch (e) { setError(e.message); }
  };
  if (!m || !m.source_found) return "";
  const pct = m.rows_total ? Math.round((100 * m.rows_done) / m.rows_total) : 0;
  return html`<div class="card"><h2>Import the SQLite library</h2>
    ${m.state === "complete" ? html`<p>Imported ${num(Object.values(m.counts || {}).reduce((a, b) => a + b, 0))} rows. <a href="/" onClick=${(e) => { e.preventDefault(); location.reload(); }}>Sign in</a> with the login you had.</p>`
    : m.state === "running" ? html`<p><span class="spin">⟳</span> ${m.rows_total ? `Importing${m.table ? ` ${m.table}` : ""}… ${pct}% (${num(m.rows_done)} of ${num(m.rows_total)} rows)` : "Starting…"}</p>`
    : html`<p class="why">Found the previous SQLite database on this server's disk. This copies your photos, review history, settings and
        logins into the new database; the SQLite file is left as it is.</p>
      ${m.state === "failed" && html`<p class="problem">The last import failed: ${m.error}</p>`}
      <button class="primary" disabled=${!code} onClick=${start}>Import</button>`}
    ${error && html`<p class="error" role="alert">${error}</p>`}
  </div>`;
}

export function Setup({ onLogin }) {
  const [code, setCode] = useState("");
  const [u, setU] = useState({ username: "", password: "", again: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const create = async (e) => {
    e.preventDefault();
    if (u.password !== u.again) { setError("The passwords don't match."); return; }
    setBusy(true); setError("");
    try { onLogin(await api("/api/setup/user", { method: "POST", body: { code, username: u.username, password: u.password } })); }
    catch (err) { setError(err.status === 422 ? "Use 12+ characters for the password, and letters, digits, . _ @ - for the username." : err.message); }
    setBusy(false);
  };
  const restore = (file, key) => upload("/api/setup/restore", file, { "X-Setup-Code": code, "X-Encryption-Key": key });
  const set = (k) => (e) => setU({ ...u, [k]: e.target.value });
  return html`<div class="setup">
    <img class="logo hero" src="/static/img/logo-320.png" width="160" height="156" alt="" />
    <h1>Coconut Barometer: first-run setup</h1>
    <p>There's no login yet. Start fresh with a new one, or bring everything from a backup.</p>
    <label for="code">Setup code</label>
    <input id="code" autocomplete="off" spellcheck="false" value=${code} onInput=${(e) => setCode(e.target.value)} placeholder="XXXX-XXXX-XXXX" />
    <p class="why">In the server's log (Render: the service → Logs): "finish setup with this code". It proves you run this server.</p>
    <div class="setup-choices">
      <form class="card" onSubmit=${create}>
        <h2>Create a login</h2>
        <label for="su">Username</label>
        <input id="su" autocomplete="username" value=${u.username} onInput=${set("username")} required />
        <label for="sp">Password <span class="muted">(12+ characters)</span></label>
        <input id="sp" type="password" autocomplete="new-password" minlength="12" value=${u.password} onInput=${set("password")} required />
        <label for="sp2">Again</label>
        <input id="sp2" type="password" autocomplete="new-password" value=${u.again} onInput=${set("again")} required />
        <button class="primary" disabled=${busy || !code}>${busy ? "Creating…" : "Create and sign in"}</button>
        ${error && html`<p class="error" role="alert">${error}</p>`}
      </form>
      <${ImportSqlite} code=${code} />
      <div class="card">
        <h2>Restore a backup</h2>
        <p class="why">Brings your photos' history, review decisions, settings and logins. Sign in with your usual login afterwards.</p>
        <${RestoreBackup} send=${restore} disabled=${!code} />
      </div>
    </div>
  </div>`;
}
