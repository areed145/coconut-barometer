// The app shell: login (or first-run setup), top bar (tabs, review count, sync status), and the pages.
import { html, render, useState, useEffect, api, setCsrf, useRoute, Toasts, ago, until, photosLink, loadTheme } from "./lib.js";
import { Photos, Albums } from "./gallery.js";
import { Reviewer } from "./review.js";
import { Analytics } from "./analytics.js";
import { MapPage } from "./map.js";
import { QueuePage } from "./queue.js";
import { Weather, WEATHER_TABS } from "./weather.js";
import { Aprs } from "./aprs.js";
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
    <img class="logo hero" src="/static/img/logo-320.png" width="200" height="195" alt="" />
    <h1 class="site-name">Coconut Barometer</h1>
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
  if (!s) return null;
  const err = s.last_result?.error;
  const text = s.running ? "Syncing… (open the queue)" : err ? "Last sync failed: " + err
    : `Synced ${ago(s.last_sync)}${s.next_run ? ` · next ${until(s.next_run)}` : ""}`;
  const state = s.running ? "busy" : err ? "bad" : "ok";
  return html`<a class=${"syncbox " + state} href=${s.running ? "#/queue" : "#/settings"}
    title=${text} aria-label=${text}><span class="dot"></span></a>`;
}

function AccountMenu({ username, onLogout, onSettings }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = (e) => { if (!e.target.closest?.(".menu")) setOpen(false); };
    const esc = (e) => e.key === "Escape" && setOpen(false);
    document.addEventListener("click", close); document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("click", close); document.removeEventListener("keydown", esc); };
  }, [open]);
  return html`<div class="menu">
    <button class=${"link burger" + (onSettings ? " on" : "")} aria-label="Menu" aria-haspopup="menu" aria-expanded=${open}
      onClick=${() => setOpen(!open)}>☰</button>
    ${open && html`<div class="menu-pop" role="menu">
      <div class="who muted">${username}</div>
      <a role="menuitem" href="#/settings" onClick=${() => setOpen(false)}>Settings</a>
      <button class="link" role="menuitem" onClick=${onLogout}>Sign out</button>
    </div>`}
  </div>`;
}

const PHOTO_TABS = [["photostream", "Photostream"], ["albums", "Albums"], ["map", "Map"], ["analytics", "Analytics"], ["review", "Review"]];

function Shell({ session, onLogout }) {
  const route = useRoute();
  const [pending, setPending] = useState(null);
  const [working, setWorking] = useState(0);         // jobs the background worker is applying or processing
  useEffect(() => {
    const load = () => api("/api/review/summary").then((s) => setPending(s.to_review)).catch(() => {});
    load(); const t = setInterval(load, 30000); return () => clearInterval(t);
  }, []);
  useEffect(() => {
    const load = () => api("/api/review/jobs", { params: { since: 1e9 } })
      .then((j) => setWorking(j.applying + j.processing + (j.syncing ? 1 : 0))).catch(() => {});
    load(); const t = setInterval(load, 10000); return () => clearInterval(t);
  }, []);
  const tab = route[0];
  useEffect(() => { if (tab === "review") location.replace("#/photos/review"); }, [tab]);   // the old top-level address
  // On narrow screens the tab rows scroll sideways: keep the selected tab in view.
  useEffect(() => {
    requestAnimationFrame(() => document.querySelectorAll("nav.tabs a.on, .subtabs a.on, .admin .sections button.on")
      .forEach((el) => el.scrollIntoView({ block: "nearest", inline: "nearest" })));
  }, [location.hash]);
  const logout = async () => { try { await api("/api/auth/logout", { method: "POST" }); } finally { onLogout(); } };
  // Photos is a section: #/photos/<photostream|albums|map|analytics|review>. An album's photos are the
  // photostream filtered to it, so they're listed under Albums.
  const inPhotos = !["weather", "aprs", "queue", "settings"].includes(tab);
  const sub = tab === "photos" && route[1] || "photostream";
  let page;
  if (!inPhotos) page = null;
  else if (sub === "albums") page = html`<${Albums} />`;
  else if (sub === "analytics") page = html`<${Analytics} tab=${route[2]} />`;
  else if (sub === "review") page = html`<${Reviewer} onCount=${setPending} targetPhotoId=${route.params.photo_id} />`;
  else if (sub === "map") page = html`<${MapPage} at=${route[2]} />`;
  else page = html`<${Photos} params=${route.params} />`;
  if (tab === "weather") page = html`<${Weather} tab=${route[1]} params=${route.params} />`;
  else if (tab === "aprs") page = html`<${Aprs} />`;
  else if (tab === "queue") page = html`<${QueuePage} />`;
  else if (tab === "settings") page = html`<${Admin} />`;
  const link = (id, label, extra) => html`<a href=${"#/" + id} class=${(id === "photos" ? inPhotos : tab === id) ? "on" : ""}>${label}${extra}</a>`;
  const subSection = sub === "photostream" && route.params.album ? "albums" : sub;
  const subtabs = inPhotos ? html`
    <nav class="subtabs" aria-label="Photos">${PHOTO_TABS.map(([t, label]) => html`<a href=${"#/photos/" + t}
      class=${t === subSection ? "on" : ""}>${label}${t === "review" && pending ? html`<span class="badge">${pending}</span>` : ""}</a>`)}</nav>`
    : tab === "weather" ? html`
    <nav class="subtabs" aria-label="Weather">${WEATHER_TABS.map(([t, label]) => html`<a href=${"#/weather/" + t}
      class=${t === (route[1] || "live") ? "on" : ""}>${label}</a>`)}</nav>` : "";
  return html`
    <header class="top">
      <a class="brand" href="#/photos" aria-label="Coconut Barometer: Photos">
        <img class="logo" src="/static/img/logo-96.png" width="46" height="45" alt="" />
        <span class="brand-name">Coconut Barometer</span></a>
      <nav class="tabs" aria-label="Pages">
        ${link("photos", "Photos", pending ? html`<span class="badge">${pending}</span>` : "")}${link("weather", "Weather")}${link("aprs", "APRS")}
        ${link("queue", "Queue", working ? html`<span class="badge busy" title="Jobs running or waiting">${working}</span>` : "")}
      </nav>
      <div class="account">
        <${SyncBox} />
        <${AccountMenu} username=${session.username} onLogout=${logout} onSettings=${tab === "settings"} />
      </div>
    </header>
    <main class="page">${subtabs}${page}</main>`;
}

function App() {
  const [session, setSession] = useState(undefined);     // undefined = checking, null = signed out
  const [setup, setSetup] = useState(false);              // no login yet: first-run setup
  const signedIn = (s) => { setCsrf(s.csrf); setSetup(false); loadTheme().then(() => setSession(s)); };
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
