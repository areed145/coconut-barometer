// Shared helpers: Preact + htm, the API client, a hash router, formatting, toasts.
export { html, render, useState, useEffect, useRef, useMemo, useCallback, useContext, createContext }
  from "../vendor/preact-htm.module.js";
import { html, useState, useEffect } from "../vendor/preact-htm.module.js";

// ---- API ----------------------------------------------------------------------
let csrf = null;
export const setCsrf = (token) => { csrf = token; };
// Signed in (a session's CSRF token is known): Photos, Weather and APRS are public; the rest needs a login.
export const signedIn = () => csrf !== null;

export class ApiError extends Error {
  constructor(status, detail, headers) { super(detail || `HTTP ${status}`); this.status = status; this.headers = headers; }
}

export async function api(path, { method = "GET", body, params } = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params || {})) if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  const headers = { Accept: "application/json" };
  if (method !== "GET") { headers["Content-Type"] = "application/json"; if (csrf) headers["X-CSRF-Token"] = csrf; }
  const r = await fetch(url, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (r.status === 401 && csrf && !path.startsWith("/api/auth/login")) window.dispatchEvent(new Event("cb:logged-out"));
  if (!r.ok) {
    const detail = Array.isArray(data?.detail) ? data.detail.map((d) => (typeof d === "string" ? d : d.msg)).join("; ") : data?.detail;
    throw new ApiError(r.status, detail, r.headers);
  }
  return data;
}

// POST, then save the response body as a file (e.g. a backup).
export async function download(path, body, filename) {
  const r = await fetch(path, { method: "POST", credentials: "same-origin", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" } });
  if (!r.ok) throw new ApiError(r.status, (await r.json().catch(() => ({}))).detail, r.headers);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(await r.blob()); a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}

// POST a file as the raw body (e.g. a backup to restore). Header values are URI-encoded,
// since headers can't carry every character (a password might).
export async function upload(path, file, extra = {}) {
  const headers = { "Content-Type": "application/octet-stream", "X-CSRF-Token": csrf || "" };
  for (const [k, v] of Object.entries(extra)) if (v) headers[k] = encodeURIComponent(v);
  const r = await fetch(path, { method: "POST", credentials: "same-origin", body: file, headers });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(r.status, data.detail, r.headers);
  return data;
}

// After a restore the server restarts itself: wait until it answers again, then reload.
export async function waitForRestart() {
  await new Promise((ok) => setTimeout(ok, 3000));
  for (let i = 0; i < 120; i++) {
    try { if ((await fetch("/healthz", { cache: "no-store" })).ok) break; } catch { /* still restarting */ }
    await new Promise((ok) => setTimeout(ok, 2000));
  }
  location.reload();
}

// A drop zone that's also a file picker. onFiles gets the dropped / picked files.
export function DropZone({ onFiles, accept, disabled, children }) {
  const [over, setOver] = useState(false);
  const pick = (e) => { const f = [...e.target.files]; e.target.value = ""; if (f.length) onFiles(f); };
  return html`<label class=${"dropzone" + (over ? " over" : "") + (disabled ? " disabled" : "")}
      onDragOver=${(e) => { e.preventDefault(); setOver(true); }} onDragLeave=${() => setOver(false)}
      onDrop=${(e) => { e.preventDefault(); setOver(false); if (!disabled && e.dataTransfer.files.length) onFiles([...e.dataTransfer.files]); }}>
    <input type="file" multiple accept=${accept} disabled=${disabled} onChange=${pick} />
    ${children}
  </label>`;
}

// ---- the color gradient (Settings → Display → Colors), for anything showing an amount ---
let gradient = [[0xf7, 0xd1, 0x25], [0x1d, 0xab, 0x45]];
const rgbOf = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
export const loadTheme = () => api("/api/theme").then((t) => { gradient = t.colors.map(rgbOf); }).catch(() => {});
// t in 0..1 -> "rgb(…)" between the low and high colors.
export const shade = (t) => {
  const [a, b] = gradient, k = Math.max(0, Math.min(1, t || 0));
  return `rgb(${a.map((x, i) => Math.round(x + (b[i] - x) * k)).join(",")})`;
};
// Where v sits between lo and hi on a log scale: 0 for the smallest, 1 for the largest.
export const logShare = (v, hi, lo = 0) => {
  const a = Math.log1p(Math.max(0, lo)), b = Math.log1p(Math.max(0, hi));
  return b > a ? (Math.log1p(Math.max(0, v)) - a) / (b - a) : 1;
};

// ---- light / dark: Auto (the device's setting), or chosen in the top bar ----------------
const THEME_KEY = "cb-theme";
export const themeChoice = () => { try { return localStorage.getItem(THEME_KEY) || "auto"; } catch { return "auto"; } };
const deviceDark = matchMedia("(prefers-color-scheme: dark)");
export const isDark = () => {
  const t = document.documentElement.getAttribute("data-theme");
  return t ? t === "dark" : deviceDark.matches;
};
const themeChanged = () => {
  document.querySelectorAll('meta[name="theme-color"]').forEach((m) => {
    m.content = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    m.removeAttribute("media");
  });
  window.dispatchEvent(new Event("cb:theme"));
};
export function setThemeChoice(choice) {
  try { choice === "auto" ? localStorage.removeItem(THEME_KEY) : localStorage.setItem(THEME_KEY, choice); } catch { /* this session only */ }
  if (choice === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", choice);
  themeChanged();
}
deviceDark.addEventListener?.("change", () => { if (themeChoice() === "auto") themeChanged(); });
// Re-render when the theme changes (for things drawn from the colors at render time).
export function useThemeTick() {
  const [tick, setTick] = useState(0);
  useEffect(() => { const on = () => setTick((n) => n + 1); window.addEventListener("cb:theme", on);
    return () => window.removeEventListener("cb:theme", on); }, []);
  return tick;
}

// ---- router: #/photos/photostream?tag=ramen&year=2019, #/photos/review, ... ------------------------------
// route is the path split on "/", with route.params the query after "?".
export function useRoute() {
  const read = () => {
    const [path, qs] = location.hash.replace(/^#\/?/, "").split("?");
    const parts = (path || "photos").split("/");
    parts.params = Object.fromEntries(new URLSearchParams(qs || ""));
    return parts;
  };
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => setRoute(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}
// Polling: fn() now and every `ms` while the page can be seen, and at once when it's shown again. A hidden
// tab (in the background, or a phone's screen off) makes no requests. Returns the cleanup, for useEffect.
export function every(fn, ms) {
  const tick = () => document.visibilityState !== "hidden" && fn();
  const shown = () => document.visibilityState === "visible" && fn();
  tick();
  const t = setInterval(tick, ms);
  document.addEventListener("visibilitychange", shown);
  return () => { clearInterval(t); document.removeEventListener("visibilitychange", shown); };
}
export const go = (path) => { location.hash = "#/" + path; };

// The Photos page filtered: photosLink({tag: "ramen"}) -> "#/photos/photostream?tag=ramen". Empty values drop out.
export const photosLink = (params = {}) => {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null)).toString();
  return "#/photos/photostream" + (qs ? "?" + qs : "");
};

// Share a page by its share link (/p/<id>, /a/<id>: the server gives those link previews): the device's
// share sheet where there is one, otherwise the link is copied.
export async function share(path, title) {
  const url = location.origin + path;
  try {
    if (navigator.share) await navigator.share({ title, url });
    else { await navigator.clipboard.writeText(url); toast("Link copied"); }
  } catch (e) { if (e.name !== "AbortError") toast("Couldn't share: " + e.message); }
}

// ---- formatting --------------------------------------------------------------------
const nf = new Intl.NumberFormat();
export const num = (n) => (n === null || n === undefined ? "–" : nf.format(n));
export const day = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "");
export function ago(ts) {
  if (!ts) return "never";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  const m = Math.round(s / 60); if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60); if (h < 36) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}
export function until(ts) {
  if (!ts) return "";
  const m = Math.round((ts - Date.now() / 1000) / 60);
  return m <= 0 ? "now" : m < 60 ? `in ${m} min` : `in ${Math.round(m / 60)} h`;
}
export const plain = (htmlText) => {   // Flickr descriptions/comments may contain HTML: show text only
  const d = new DOMParser().parseFromString(htmlText || "", "text/html");
  return d.body.textContent || "";
};
// Best available size from a photo's sizes map, by preference.
export const pick = (sizes, prefs) => prefs.map((k) => sizes?.[k]).find(Boolean) || "";
// For <img onError>: Flickr only has the larger sizes if the original is big enough,
// so when one fails, step down to the next size in the same preference list.
export const fallback = (sizes, prefs) => (e) => {
  const urls = prefs.map((k) => sizes?.[k]).filter(Boolean);
  const next = urls[urls.indexOf(e.target.currentSrc || e.target.src) + 1];
  if (next) { e.target.removeAttribute("srcset"); e.target.src = next; }
};

// ---- toasts ----------------------------------------------------------------------------
let pushToast = () => {};
export const toast = (msg) => pushToast(msg);
export function Toasts() {
  const [msg, setMsg] = useState(null);
  useEffect(() => {
    let t;
    pushToast = (m) => { setMsg(m); clearTimeout(t); t = setTimeout(() => setMsg(null), 3200); };
    return () => clearTimeout(t);
  }, []);
  return msg ? html`<div class="toast" role="status">${msg}</div>` : null;
}
