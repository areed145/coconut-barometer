// Small charts for the Analytics tabs, drawn as plain HTML/SVG (no library): bar lists
// (colored along the Settings → Display → Colors gradient), photo tiles and a week heatmap. Nothing uses inline
// HTML or style attributes in markup, so the Content-Security-Policy holds.
import { html, useState, useMemo, num, pick, shade, logShare } from "./lib.js";
import { Lightbox } from "./gallery.js";

export const asPhoto = (r) => ({ id: r.photo_id || r.id, title: r.title, sizes: r.sizes || {}, views: r.views,
  faves: r.faves ?? r.favorites, comments: r.comments });
export const pct = (x) => (x == null ? "–" : `${x > 0 ? "+" : ""}${Math.round(x * 100)}%`);
export const times = (x) => (x == null ? "–" : `${x.toFixed(x >= 10 ? 0 : 2)}×`);

// The lightbox for a list of photo rows: [element, open(rows, index)].
export function usePhotoBox() {
  const [box, setBox] = useState(null);
  const el = box && html`<${Lightbox} photos=${box.photos} index=${box.index} onIndex=${(i) => setBox({ ...box, index: i })}
      onClose=${() => setBox(null)} />`;
  return [el, (rows, index) => setBox({ photos: rows.map(asPhoto), index })];
}

export function TopGrid({ rows, caption, onOpen, empty = "Nothing here yet." }) {
  if (!rows?.length) return html`<p class="muted">${empty}</p>`;
  return html`<div class="toplist">${rows.map((r, i) => html`<button class="tile" key=${r.photo_id || r.id} onClick=${() => onOpen(rows, i)}
      aria-label=${r.title || "Untitled photo"} title=${r.title || ""}>
    <img loading="lazy" alt="" src=${pick(r.sizes, ["n", "w", "q"])} />
    <span class="cap">${caption(r)}</span></button>`)}</div>`;
}

// rows: [{label, ...}], value: the key bars are sized by; right: text shown at the end.
// Length is linear; color runs along the gradient from the smallest bar to the largest, on a
// log scale like the map's clusters, so the differences show even when all the values are big.
// after(r): an optional extra column at the very end (e.g. a trend mark).
export function Bars({ rows, value = "views", right, after, title, onPick, empty = "No data yet.", sorted = false }) {
  if (!rows?.length) return html`<p class="muted">${empty}</p>`;
  const list = sorted ? [...rows].sort((a, b) => (b[value] || 0) - (a[value] || 0)) : rows;
  const max = Math.max(...list.map((r) => r[value] || 0), 1e-9);
  const min = Math.min(...list.map((r) => Math.max(0, r[value] || 0)));
  return html`<div class="bars">${list.map((r) => {
    const v = Math.max(0, r[value] || 0);
    return html`<div class=${"bar" + (onPick ? " pick" : "")} key=${r.label}
        title=${title ? title(r) : ""} onClick=${onPick ? () => onPick(r) : undefined}>
      <span class="label">${r.label}</span>
      <span class="track"><span class="fill" style=${`width:${(100 * v) / max}%;background:${shade(logShare(v, max, min))}`}></span></span>
      <span class="n">${right ? right(r) : num(r[value])}</span>${after && html`<span class="after">${after(r)}</span>`}</div>`;
  })}</div>`;
}

// A sortable table. cols: [{key, label, fmt?, num?}]
export function Table({ rows, cols, onPick, initial }) {
  const [sort, setSort] = useState(initial || cols.find((c) => c.num)?.key);
  const [desc, setDesc] = useState(true);
  const sorted = useMemo(() => [...rows].sort((a, b) => {
    const x = a[sort], y = b[sort];
    const c = typeof x === "string" || typeof y === "string" ? String(x ?? "").localeCompare(String(y ?? "")) : (x ?? -Infinity) - (y ?? -Infinity);
    return desc ? -c : c;
  }), [rows, sort, desc]);
  const click = (k) => { if (k === sort) setDesc(!desc); else { setSort(k); setDesc(true); } };
  return html`<div class="tablewrap"><table class="data">
    <thead><tr>${cols.map((c) => html`<th class=${c.num ? "num" : ""}><button class="link" onClick=${() => click(c.key)}>
      ${c.label}${sort === c.key ? (desc ? " ↓" : " ↑") : ""}</button></th>`)}</tr></thead>
    <tbody>${sorted.map((r, i) => html`<tr key=${i} class=${onPick ? "pick" : ""} onClick=${onPick ? () => onPick(r) : undefined}>
      ${cols.map((c) => html`<td class=${c.num ? "num" : ""}>${c.fmt ? c.fmt(r[c.key], r) : c.num ? num(r[c.key]) : r[c.key]}</td>`)}</tr>`)}
    </tbody></table></div>`;
}

// 7 rows × 24 columns of counts; rows/cols are labels.
export function Heatmap({ cells, rows, cols }) {
  const max = Math.max(...cells.flat(), 1);
  return html`<div class="heat">
    <span></span>${cols.map((c, i) => html`<span class="hcol">${i % 3 === 0 ? c : ""}</span>`)}
    ${rows.map((r, y) => html`<span class="hrow">${r}</span>${cells[y].map((v, x) => html`<span
        class=${"hcell l" + Math.min(5, Math.ceil((5 * v) / max))} title=${`${r} ${cols[x]}:00 · ${num(v)}`}></span>`)}`)}
  </div>`;
}

// A number with its change against the period before.
export function Kpi({ label, now, before, note }) {
  const d = before ? (now - before) / before : null;
  return html`<div class="kpi"><span>${label}</span><b>${num(now)}</b>
    ${d != null && html`<small class=${d >= 0 ? "up" : "down"}> ${pct(d)} vs before</small>`}${note && html`<small> ${note}</small>`}</div>`;
}
