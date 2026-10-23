// The app shell: first-run setup, top bar (tabs, review count, whether the queue is busy), and the pages: Home (#/), Photos, Weather
// and APRS are public; Review, Analytics, Queue and Settings need a login (and their tabs only show once signed in).
import { html, render, useState, useEffect, api, setCsrf, useRoute, Toasts, photosLink, loadTheme, every } from "./lib.js";
import { Home } from "./home.js";

// Every page but Home (the landing page, which comes with the app) loads its code the first time it's
// opened. If that fails - usually the site was updated since this tab opened - it offers a reload.
function lazy(load, name) {
  let Page = null, waiting = null;
  return function Lazy(props) {
    const [, setReady] = useState(false);
    const [failed, setFailed] = useState(false);
    useEffect(() => {
      if (Page) return;
      waiting = waiting || load().then((m) => { Page = m[name]; });
      waiting.then(() => setReady(true), () => { waiting = null; setFailed(true); });
    }, []);
    if (failed) return html`<div class="empty">This page couldn't load. <a href="" onClick=${() => location.reload()}>Reload</a></div>`;
    return Page ? html`<${Page} ...${props} />` : html`<div class="boot">Loading…</div>`;
  };
}
const Photos = lazy(() => import("./gallery.js"), "Photos");
const Albums = lazy(() => import("./gallery.js"), "Albums");
const SinglePhoto = lazy(() => import("./gallery.js"), "SinglePhoto");
const Reviewer = lazy(() => import("./review.js"), "Reviewer");
const BulkReview = lazy(() => import("./review_bulk.js"), "BulkReview");
const AppliedList = lazy(() => import("./review_bulk.js"), "AppliedList");
const Analytics = lazy(() => import("./analytics.js"), "Analytics");
const MapPage = lazy(() => import("./map.js"), "MapPage");
const QueuePage = lazy(() => import("./queue.js"), "QueuePage");
const Weather = lazy(() => import("./weather.js"), "Weather");
const Aprs = lazy(() => import("./aprs.js"), "Aprs");
const Igate = lazy(() => import("./igate.js"), "Igate");
const Admin = lazy(() => import("./admin.js"), "Admin");
const Setup = lazy(() => import("./setup.js"), "Setup");

function Login({ onLogin, note }) {
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
    ${note && html`<p class="muted">${note}</p>`}
    <label for="u">Username</label>
    <input id="u" autocomplete="username" value=${username} onInput=${(e) => setUsername(e.target.value)} required />
    <label for="p">Password</label>
    <input id="p" type="password" autocomplete="current-password" value=${password}
           onInput=${(e) => setPassword(e.target.value)} required />
    <button class="primary" disabled=${busy}>${busy ? "Signing in…" : "Sign in"}</button>
    ${error && html`<p class="error" role="alert">${error}</p>`}
  </form>`;
}

function AccountMenu({ username, onLogout, onSettings }) {     // username null: a visitor
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
    ${open && (username ? html`<div class="menu-pop" role="menu">
      <div class="who muted">${username}</div>
      <a role="menuitem" href="#/settings" onClick=${() => setOpen(false)}>Settings</a>
      <button class="link" role="menuitem" onClick=${onLogout}>Sign out</button>
    </div>` : html`<div class="menu-pop" role="menu">
      <a role="menuitem" href=${"#/login?next=" + encodeURIComponent(location.hash || "#/")}
         onClick=${() => setOpen(false)}>Sign in</a>
    </div>`)}
  </div>`;
}

const PHOTO_TABS = [["photostream", "Photostream"], ["albums", "Albums"], ["map", "Map"], ["analytics", "Analytics"], ["review", "Review"]];
const PRIVATE_PHOTO_TABS = ["analytics", "review"];
const PRIVATE_TABS = ["queue", "settings"];
const APRS_TABS = [["tracking", "Tracking"], ["igate", "Igate"]];
const WEATHER_TABS = [["live", "Live"], ["history", "History"], ["records", "Records"], ["map", "Map"]];

function Shell({ session, onLogin, onLogout }) {       // session null: a visitor, who sees the public pages
  const route = useRoute();
  const [pending, setPending] = useState(null);
  const [queued, setQueued] = useState(0);           // jobs on the background worker: running or waiting
  const [health, setHealth] = useState(null);        // the worst connection's state, by the logo (Home has the detail)
  useEffect(() => every(() => api("/api/status").then((s) => setHealth(s)).catch(() => {}), 60000), [session]);
  useEffect(() => {
    if (!session) { setPending(null); return; }
    const load = () => api("/api/review/summary").then((s) => setPending(s.to_review)).catch(() => {});
    return every(load, 30000);
  }, [session]);
  useEffect(() => {
    if (!session) { setQueued(0); return; }
    const load = () => api("/api/review/jobs", { params: { since: 1e9 } }).then((j) => setQueued(j.queued)).catch(() => {});
    return every(load, 5000);
  }, [session]);
  const tab = route[0];
  // On narrow screens the tab rows scroll sideways: keep the selected tab in view.
  useEffect(() => {
    requestAnimationFrame(() => document.querySelectorAll("nav.tabs a.on, .subtabs a.on, .admin .sections button.on")
      .forEach((el) => el.scrollIntoView({ block: "nearest", inline: "nearest" })));
  }, [location.hash]);
  const logout = async () => { try { await api("/api/auth/logout", { method: "POST" }); } finally { onLogout(); } };
  // Photos is a section: #/photos/<photostream|albums|map|analytics|review>. An album's photos are the
  // photostream filtered to it, so they're listed under Albums.
  const inPhotos = !["home", "weather", "aprs", "queue", "settings", "login"].includes(tab);
  const sub = tab === "photos" && route[1] || "photostream";
  const next = route.params.next || "#/";
  useEffect(() => { if (session && tab === "login") location.replace(next); }, [session, tab]);
  let page;
  if (!inPhotos) page = null;
  else if (sub === "albums") page = html`<${Albums} />`;
  else if (sub === "analytics") page = html`<${Analytics} tab=${route[2]} />`;
  else if (sub === "review" && route[2] === "bulk") page = html`<${BulkReview} />`;
  else if (sub === "review" && route[2] === "applied") page = html`<${AppliedList} />`;
  else if (sub === "review") page = html`<${Reviewer} onCount=${setPending} targetPhotoId=${route.params.photo_id} />`;
  else if (sub === "map") page = html`<${MapPage} at=${route[2]} />`;
  else if (sub === "photo") page = html`<${SinglePhoto} id=${route[2]} />`;
  else page = html`<${Photos} params=${route.params} />`;
  if (tab === "home") page = html`<${Home} session=${session} pending=${pending} queued=${queued} />`;
  else if (tab === "weather") page = html`<${Weather} tab=${route[1]} params=${route.params} />`;
  else if (tab === "aprs") page = route[1] === "igate" ? html`<${Igate} />` : html`<${Aprs} />`;
  else if (tab === "queue") page = html`<${QueuePage} />`;
  else if (tab === "settings") page = html`<${Admin} at=${route[1]} />`;
  if (!session && tab === "login") page = html`<${Login} onLogin=${(s) => { onLogin(s); location.replace(next); }} />`;
  else if (!session && (PRIVATE_TABS.includes(tab) || (inPhotos && PRIVATE_PHOTO_TABS.includes(sub))))
    page = html`<${Login} onLogin=${onLogin} note="Sign in to see this page." />`;
  const link = (id, label, extra) => html`<a href=${"#/" + id} class=${(id === "photos" ? inPhotos : tab === id) ? "on" : ""}>${label}${extra}</a>`;
  const subSection = sub === "photostream" && route.params.album ? "albums" : sub === "photo" ? "photostream" : sub;
  const subtabs = inPhotos ? html`
    <nav class="subtabs" aria-label="Photos">${PHOTO_TABS.filter(([t]) => session || !PRIVATE_PHOTO_TABS.includes(t)).map(([t, label]) => html`<a href=${"#/photos/" + t}
      class=${t === subSection ? "on" : ""}>${label}${t === "review" && pending ? html`<span class="badge">${pending}</span>` : ""}</a>`)}</nav>`
    : tab === "weather" ? html`
    <nav class="subtabs" aria-label="Weather">${WEATHER_TABS.map(([t, label]) => html`<a href=${"#/weather/" + t}
      class=${t === (route[1] || "live") ? "on" : ""}>${label}</a>`)}</nav>`
    : tab === "aprs" ? html`
    <nav class="subtabs" aria-label="APRS">${APRS_TABS.map(([t, label]) => html`<a href=${"#/aprs/" + t}
      class=${t === (route[1] || "tracking") ? "on" : ""}>${label}</a>`)}</nav>` : "";
  return html`
    <header class="top">
      <a class="brand" href="#/" aria-label="Coconut Barometer: Home">
        <img class="logo" src="/static/img/logo-96.png" width="46" height="45" alt="" />
        <span class="brand-name">Coconut Barometer</span>
        ${health && (health.overall === "warn" || health.overall === "bad") && html`<i class=${"health-dot " + health.overall}
          title=${health.rows.filter((r) => r.level === health.overall).map((r) => `${r.name}: ${r.state}`).join("\n")}
          role="img" aria-label=${`Status: ${health.overall === "bad" ? "something's down" : "something needs a look"}`}></i>`}</a>
      <nav class="tabs" aria-label="Pages">
        ${link("photos", "Photos", pending ? html`<span class="badge">${pending}</span>` : "")}${link("weather", "Weather")}${link("aprs", "APRS")}
        ${session && link("queue", "Queue", html`<span class=${"qdot" + (queued ? " busy" : "")} role="status"
          title=${queued ? `${queued} job${queued === 1 ? "" : "s"} running or waiting` : "Nothing queued"}
          aria-label=${queued ? `${queued} queued` : "nothing queued"}></span>`)}
      </nav>
      <div class="account">
        <${AccountMenu} username=${session?.username} onLogout=${logout} onSettings=${tab === "settings"} />
      </div>
    </header>
    <main class="page">${subtabs}${page}</main>`;
}

function App() {
  const [session, setSession] = useState(undefined);     // undefined = checking, null = a visitor (signed out)
  const [setup, setSetup] = useState(false);              // no login yet: first-run setup
  const signedIn = (s) => { setCsrf(s.csrf); setSetup(false); loadTheme().then(() => setSession(s)); };
  const signedOut = () => { setCsrf(null); loadTheme().then(() => setSession(null)); };
  useEffect(() => {
    api("/api/auth/session").then(signedIn).catch(() => {
      api("/api/setup").then((s) => setSetup(s.needed)).catch(() => {}).finally(signedOut);
    });
    window.addEventListener("cb:logged-out", signedOut);
    return () => window.removeEventListener("cb:logged-out", signedOut);
  }, []);
  return html`${session === undefined ? html`<div class="boot">Loading…</div>`
    : setup ? html`<${Setup} onLogin=${signedIn} />`
    : html`<${Shell} session=${session} onLogin=${signedIn} onLogout=${signedOut} />`}
    <${Toasts} />`;
}

// A share link (/p/<id>, /a/<id>) is its own page only for the link preview; the app lives at / under #/.
const shared = location.pathname.match(/^\/([pa])\/(\d+)\/?$/);
if (shared) history.replaceState(null, "", "/" + (shared[1] === "p" ? `#/photos/photo/${shared[2]}` : photosLink({ album: shared[2] })));
render(html`<${App} />`, document.getElementById("app"));
