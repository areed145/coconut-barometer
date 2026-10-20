// Shared helpers: Preact + htm, the API client, a hash router, formatting, toasts.
export { html, render, useState, useEffect, useRef, useMemo, useCallback, useContext, createContext }
  from "../vendor/preact-htm.module.js";
import { html, useState, useEffect } from "../vendor/preact-htm.module.js";

// ---- API ----------------------------------------------------------------------
let csrf = null;
export const setCsrf = (token) => { csrf = token; };

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
  if (r.status === 401 && !path.startsWith("/api/auth/login")) window.dispatchEvent(new Event("fo:logged-out"));
  if (!r.ok) {
    const detail = Array.isArray(data?.detail) ? data.detail.map((d) => d.msg).join("; ") : data?.detail;
    throw new ApiError(r.status, detail, r.headers);
  }
  return data;
}

// ---- router: #/photos, #/albums/123, ... -------------------------------------------
export function useRoute() {
  const read = () => (location.hash.replace(/^#\/?/, "") || "photos").split("/");
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => setRoute(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}
export const go = (path) => { location.hash = "#/" + path; };

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
