// The app shell: login (or first-run setup), top bar (tabs, review count, sync status), and the pages.
import { html, render, useState, useEffect, api, setCsrf, useRoute, Toasts, toast, ago, until, photosLink } from "./lib.js";
import { Photos, Albums } from "./gallery.js";
import { Reviewer } from "./review.js";
import { Analytics } from "./analytics.js";
import { MapPage } from "./map.js";
import { QueuePage } from "./queue.js";
import { Admin } from "./admin.js";
import { Setup } from "./setup.js";

function Login({ onLogin }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError("");
    try {
      const s = await api("/api/auth/login", { method: "POST", body: { username, password } });
      setPassword(""); onLogin(s);
    } catch (err) {
      const wait = err.headers?.get?.("Retry-After");
      setError(err.status === 429 ? `Too many failed attempts. Try again in ${Math.ceil((wait || 900) / 60)} min.`
        : err.status === 401 ? "Wrong username or password." : `Couldn't log in (${err.message}).`);
    }
    setBusy(false);
  };
  return html`<form class="login" onSubmit=${submit}>
    <h1>Flickr Organizer</h1>
    <label for="u">Username</label>
    <input id="u" autocomplete="username" value=${username} onInput=${(e) => setUsername(e.target.value)} required />
    <label for="p">Password</label>
    <input id="p" type="password" autocomplete="current-password" value=${password}
           onInput=${(e) => setPassword(e.target.value)} required />
    <button class="primary" disabled=${busy}>${busy ? "Signing in…" : "Sign in"}</button>
    ${error && html`<p class="error" role="alert">${error}</p>`}
  </form>`;
}

function SyncBox() {
  const [s, setS] = useState(null);
  const load = () => api("/api/sync/status").then(setS).catch(() => {});
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, []);
  const run = async () => {
    try { const r = await api("/api/sync/run", { method: "POST", body: {} });
      toast(r.status === "busy" ? "A sync is already running." : r.status === "no_flickr"
        ? "Set the Flickr API keys in Settings first." : "Sync started."); load();
    } catch (e) { toast("Couldn't start a sync: " + e.message); }
  };
  if (!s) return null;
  const err = s.last_result?.error;
  return html`<div class="syncbox" title=${err ? "Last sync failed: " + err : `Due next: ${s.due.join(" + ")}`}>
    ${s.running ? html`<a href="#/queue" title="Open the queue"><span class="spin">⟳</span> syncing…</a>`
      : html`<span>${err ? "⚠ sync failed" : "synced " + ago(s.last_sync)}${s.next_run ? ` · next ${until(s.next_run)}` : ""}</span>`}
    <button class="link" onClick=${run} disabled=${s.running}>Sync now</button>
  </div>`;
}

function Shell({ session, onLogout }) {
  const route = useRoute();
  const [pending, setPending] = useState(null);
  useEffect(() => {
    const load = () => api("/api/review/summary").then((s) => setPending(s.to_review)).catch(() => {});
    load(); const t = setInterval(load, 30000); return () => clearInterval(t);
  }, []);
  const tab = route[0];
  const logout = async () => { try { await api("/api/auth/logout", { method: "POST" }); } finally { onLogout(); } };
  let page;
  useEffect(() => {     // old album links (#/albums/123) are the Photos page filtered to the album
    if (tab === "albums" && route[1]) location.replace(photosLink({ album: route[1] }));
  }, [tab, route[1]]);
  if (tab === "albums" && !route[1]) page = html`<${Albums} />`;
  else if (tab === "review") page = html`<${Reviewer} onCount=${setPending} />`;
  else if (tab === "analytics") page = html`<${Analytics} tab=${route[1]} />`;
  else if (tab === "map") page = html`<${MapPage} at=${route[1]} />`;
  else if (tab === "queue") page = html`<${QueuePage} />`;
  else if (tab === "settings") page = html`<${Admin} />`;
  else page = html`<${Photos} params=${route.params} />`;
  // An album's photos are under Albums; every other photo list under Photos.
  const section = tab === "photos" && route.params.album ? "albums" : tab;
  const link = (id, label, extra) => html`<a href=${"#/" + id} class=${section === id || (id === "photos" && !["albums", "map", "review", "queue", "analytics", "settings"].includes(section)) ? "on" : ""}>${label}${extra}</a>`;
  return html`
    <header class="top">
      <a class="brand" href="#/photos">Flickr Organizer</a>
      <nav class="tabs">
        ${link("photos", "Photos")}${link("albums", "Albums")}${link("map", "Map")}
        ${link("review", "Review", pending ? html`<span class="badge">${pending}</span>` : "")}
        ${link("analytics", "Analytics")}${link("settings", "Settings")}
      </nav>
      <span class="spacer"></span>
      <${SyncBox} />
      <span class="muted">${session.username}</span>
      <button class="link" onClick=${logout}>Sign out</button>
    </header>
    <main class="page">${page}</main>`;
}

function App() {
  const [session, setSession] = useState(undefined);     // undefined = checking, null = signed out
  const [setup, setSetup] = useState(false);              // no login yet: first-run setup
  const signedIn = (s) => { setCsrf(s.csrf); setSetup(false); setSession(s); };
  const signedOut = () => { setCsrf(null); setSession(null); };
  useEffect(() => {
    api("/api/auth/session").then(signedIn).catch(() => {
      api("/api/setup").then((s) => setSetup(s.needed)).catch(() => {}).finally(signedOut);
    });
    window.addEventListener("fo:logged-out", signedOut);
    return () => window.removeEventListener("fo:logged-out", signedOut);
  }, []);
  return html`${session === undefined ? html`<div class="boot">Loading…</div>`
    : session ? html`<${Shell} session=${session} onLogout=${signedOut} />`
    : setup ? html`<${Setup} onLogin=${signedIn} />` : html`<${Login} onLogin=${signedIn} />`}
    <${Toasts} />`;
}

render(html`<${App} />`, document.getElementById("app"));
