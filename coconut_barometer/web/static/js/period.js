// The period a page reports on (periods.py): one of its preset buttons (7 days … All), or a custom one picked as two
// dates, with shortcuts to a month or a year. It's a token, kept in the address (?range=30d, ?range=2025-01-01..2025-03-31)
// so a link opens the same view, and remembered per section in this browser so the next visit starts where you left off.
import { html, useRoute } from "./lib.js";

// [token, label, days]
export const PRESETS = [["7d", "7 days", 7], ["30d", "30 days", 30], ["90d", "90 days", 90], ["1y", "1 year", 365], ["all", "All", 3650]];
const DATES = /^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/;
const NAMES = { 1: "the last 24 hours", 7: "the last 7 days", 30: "the last 30 days", 90: "the last 90 days", 365: "the last year", 3650: "all time" };

const iso = (d) => d.toLocaleDateString("sv");             // a local date as YYYY-MM-DD
const at = (s) => new Date(`${s}T00:00:00`);
const shift = (s, days) => { const d = at(s); d.setDate(d.getDate() + days); return iso(d); };
export const today = () => iso(new Date());

// {start, end} of a custom token, else null.
export function dates(token) {
  const m = DATES.exec(token || "");
  return m && m[1] <= m[2] ? { start: m[1], end: m[2] } : null;
}

const preset = (token, presets) => presets.find(([t]) => t === token);
export const isPeriod = (token, presets = PRESETS, custom = true) => !!(preset(token, presets) || (custom && dates(token)));

// What the API takes: {days} for a preset, {start, end} for custom dates.
export function periodParams(token, presets = PRESETS) {
  const d = dates(token);
  return d ? { start: d.start, end: d.end } : { days: (preset(token, presets) || presets[1])[2] };
}

// [start, end] in epoch seconds: custom dates from the first day's midnight to the end of the last (or now), else the
// last N days to now. For APIs that take times (APRS).
export function periodSpan(token, presets = PRESETS) {
  const now = Math.floor(Date.now() / 1000), d = dates(token);
  if (!d) return [now - (preset(token, presets) || presets[1])[2] * 86400, now];
  return [Math.floor(at(d.start).getTime() / 1000), Math.min(now, Math.floor(at(shift(d.end, 1)).getTime() / 1000) - 1)];
}

// In words, for headings and the poster: "the last 30 days", "all time", "2025", "March 2025", "Mar 3 – Apr 9, 2025".
export function periodName(token, presets = PRESETS) {
  const d = dates(token);
  if (!d) return NAMES[(preset(token, presets) || presets[1])[2]] || `the last ${(preset(token, presets) || presets[1])[2]} days`;
  const a = at(d.start), b = at(d.end), now = today(), sameYear = a.getFullYear() === b.getFullYear();
  const so = (end) => (d.end === now && end !== now ? " so far" : "");
  const yearEnd = `${b.getFullYear()}-12-31`, monthEnd = iso(new Date(b.getFullYear(), b.getMonth() + 1, 0));
  if (d.start.endsWith("-01-01") && sameYear && (d.end === yearEnd || d.end === now)) return `${a.getFullYear()}${so(yearEnd)}`;
  if (d.start.endsWith("-01") && d.start.slice(0, 7) === d.end.slice(0, 7) && (d.end === monthEnd || d.end === now))
    return `${a.toLocaleDateString([], { month: "long", year: "numeric" })}${so(monthEnd)}`;
  const day = (x, year) => x.toLocaleDateString([], { month: "short", day: "numeric", ...(year ? { year: "numeric" } : {}) });
  if (d.start === d.end) return day(a, true);
  return `${day(a, !sameYear)} – ${day(b, true)}`;
}

// A short name for a file: "30-days", "all-time", "2025-03-01-to-2025-03-31".
export const periodFile = (token) => (dates(token) ? `${dates(token).start}-to-${dates(token).end}`
  : token === "all" ? "all-time" : token.replace(/^(\d+)d$/, "$1-days").replace(/^1y$/, "1-year"));

// [token, setToken]: ?range= in the address, else what was picked last in this section (`key`), else `fallback`.
// Setting it replaces the address (no history entry per click) and remembers it.
export function usePeriod(key, { presets = PRESETS, fallback = "30d", custom = true } = {}) {
  const route = useRoute();
  const storeKey = `period:${key}`;
  let stored = null;
  try { stored = localStorage.getItem(storeKey); } catch { /* private window: this visit only */ }
  const token = isPeriod(route.params.range, presets, custom) ? route.params.range : isPeriod(stored, presets, custom) ? stored : fallback;
  const set = (next) => {
    try { localStorage.setItem(storeKey, next); } catch { /* this visit only */ }
    const [path, qs] = location.hash.split("?");
    const q = new URLSearchParams(qs || "");
    q.set("range", next);
    location.replace(`${path || "#/"}?${q}`);
  };
  return [token, set];
}

// Shortcuts in the custom picker: this and last month, this and last year, then each year back to `firstYear`.
function shortcuts(firstYear) {
  const now = new Date(), y = now.getFullYear(), m = now.getMonth(), t = today();
  const month = (yy, mm) => [iso(new Date(yy, mm, 1)), iso(new Date(yy, mm + 1, 0))];
  const out = [["This month", iso(new Date(y, m, 1)), t], ["Last month", ...month(y, m - 1)],
    ["This year", `${y}-01-01`, t], [String(y - 1), `${y - 1}-01-01`, `${y - 1}-12-31`]];
  for (let yy = y - 2; yy >= Math.min(y - 2, firstYear || y - 4); yy--) out.push([String(yy), `${yy}-01-01`, `${yy}-12-31`]);
  return out;
}

// The buttons, and with Custom picked, the two dates and the shortcuts. firstYear: the earliest year worth offering.
export function Period({ value, onChange, presets = PRESETS, custom = true, firstYear, label = "Period" }) {
  const d = custom && dates(value);
  const pickDates = (start, end) => onChange(`${start}..${end > today() ? today() : end}`);
  const startCustom = () => {
    if (d) return;
    const days = Math.min(365, (preset(value, presets) || presets[1])[2]);
    pickDates(shift(today(), 1 - days), today());
  };
  const jump = (e) => {
    const s = shortcuts(firstYear).find(([name]) => name === e.target.value);
    if (s) pickDates(s[1], s[2]);
    e.target.value = "";
  };
  return html`<span class="period" role="group" aria-label=${label}>
    <span class="range">${presets.map(([t, text]) => html`<button key=${t} class=${value === t ? "primary" : ""} aria-pressed=${value === t}
        onClick=${() => onChange(t)}>${text}</button>`)}
      ${custom && html`<button class=${d ? "primary" : ""} aria-pressed=${!!d} onClick=${startCustom}>Custom</button>`}</span>
    ${d && html`<span class="daterange">
      <input type="date" aria-label="From" value=${d.start} max=${d.end} onChange=${(e) => e.target.value && pickDates(e.target.value, d.end)} />
      <span class="muted">to</span>
      <input type="date" aria-label="To" value=${d.end} min=${d.start} max=${today()} onChange=${(e) => e.target.value && pickDates(d.start, e.target.value)} />
      <select aria-label="Jump to a month or year" value="" onChange=${jump}>
        <option value="">Jump to…</option>
        ${shortcuts(firstYear).map(([name]) => html`<option key=${name} value=${name}>${name}</option>`)}
      </select></span>`}</span>`;
}
