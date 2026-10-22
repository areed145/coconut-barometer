// First run (no login yet): create one, or restore a backup. Also the restore form in Settings.
import { html, useState, api, upload, waitForRestart, DropZone } from "./lib.js";

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
      server doesn't have, so you'll need to set them again in Settings.</p>`}</div>`;
  return html`<div class="restore">
    <${DropZone} onFiles=${onFiles} accept=".gz,.db,.key" disabled=${disabled || state.busy}>
      ${file ? html`<strong>${file.name}</strong> <span class="muted">· ${(file.size / 1048576).toFixed(1)} MB · drop another to replace</span>`
        : html`<strong>Drop a backup here</strong> <span class="muted">(.db.gz from <code>coconut-barometer backup</code>), or click to choose.
          Drop <code>secret.key</code> too to bring the API keys.</span>`}
    <//>
    <label for="restore-key">Encryption key <span class="muted">(optional)</span></label>
    <input id="restore-key" type="password" autocomplete="off" spellcheck="false" value=${key} disabled=${state.busy}
           onInput=${(e) => setKey(e.target.value)} placeholder="contents of ~/.config/coconut-barometer/secret.key" />
    <p class="why">The key the backup's API keys are encrypted with, from the computer it came from. Without it they can't be read,
      and you set them again in Settings.</p>
    <button class="primary" disabled=${!file || disabled || state.busy} onClick=${go}>${state.busy ? "Uploading…" : "Restore"}</button>
    ${state.error && html`<p class="error" role="alert">${state.error}</p>`}
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
      <div class="card">
        <h2>Restore a backup</h2>
        <p class="why">Brings your photos' history, review decisions, settings and logins. Sign in with your usual login afterwards.</p>
        <${RestoreBackup} send=${restore} disabled=${!code} />
      </div>
    </div>
  </div>`;
}
