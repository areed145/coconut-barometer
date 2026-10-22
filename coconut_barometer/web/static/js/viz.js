// Small charts for the Analytics tabs, drawn as plain HTML/SVG (no library): bar lists
// (colored along the Settings → Photos → Colors gradient), photo tiles, a week heatmap and a scatter plot. Nothing uses inline
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

// ---- scatter -------------------------------------------------------------------------------
// points: [{label, x, y, r?, tone?}], log scales; quadrant guides at the medians.
export function Scatter({ points, xLabel, yLabel, onPick, labels = 18, corners }) {
  const sw = 900, sh = 380, m = { l: 56, r: 14, t: 12, b: 38 };
  const good = points.filter((p) => p.x > 0 && p.y > 0);
  if (!good.length) return html`<p class="muted">Not enough tags.</p>`;
  const lx = good.map((p) => Math.log10(p.x)), ly = good.map((p) => Math.log10(p.y));
  const [x0, x1] = [Math.min(...lx), Math.max(...lx) + 1e-9], [y0, y1] = [Math.min(...ly), Math.max(...ly) + 1e-9];
  const sx = (v) => m.l + ((Math.log10(v) - x0) / (x1 - x0)) * (sw - m.l - m.r);
  const sy = (v) => sh - m.b - ((Math.log10(v) - y0) / (y1 - y0)) * (sh - m.t - m.b);
  const med = (a) => [...a].sort((p, q) => p - q)[Math.floor(a.length / 2)];
  const mx = 10 ** med(lx), my = 10 ** med(ly);
  const ticks = (lo, hi) => { const out = []; for (let e = Math.floor(lo); e <= Math.ceil(hi); e++) out.push(10 ** e); return out; };
  // Label the most striking points (high views for how few photos), skipping any label that
  // would overlap one already placed.
  const boxes = [], named = new Set();
  for (const p of [...good].sort((a, b) => b.y / b.x - a.y / a.x)) {
    if (named.size >= labels) break;
    const x = sx(p.x) + 7, y = Math.max(m.t + 10, sy(p.y) + 4), w = 6.2 * p.label.length;
    if (x + w > sw - 2 || boxes.some((b) => x < b.x + b.w + 4 && x + w + 4 > b.x && Math.abs(y - b.y) < 13)) continue;
    boxes.push({ x, y, w });
    named.add(p.label);
  }
  return html`<svg class="scatter" viewBox=${`0 0 ${sw} ${sh}`} role="img" aria-label=${`${yLabel} by ${xLabel}`}>
    ${ticks(x0, x1).filter((v) => v >= 10 ** x0 / 1.01 && v <= 10 ** x1 * 1.01).map((v) => html`<g key=${"x" + v}>
      <line class="grid" x1=${sx(v)} x2=${sx(v)} y1=${m.t} y2=${sh - m.b} /><text class="axis" x=${sx(v)} y=${sh - m.b + 16} text-anchor="middle">${num(v)}</text></g>`)}
    ${ticks(y0, y1).filter((v) => v >= 10 ** y0 / 1.01 && v <= 10 ** y1 * 1.01).map((v) => html`<g key=${"y" + v}>
      <line class="grid" x1=${m.l} x2=${sw - m.r} y1=${sy(v)} y2=${sy(v)} /><text class="axis" x=${m.l - 6} y=${sy(v) + 4} text-anchor="end">${num(v)}</text></g>`)}
    <line class="median" x1=${sx(mx)} x2=${sx(mx)} y1=${m.t} y2=${sh - m.b} />
    <line class="median" x1=${m.l} x2=${sw - m.r} y1=${sy(my)} y2=${sy(my)} />
    ${good.map((p) => html`<circle key=${p.label} cx=${sx(p.x)} cy=${sy(p.y)} r=${p.r || 4} class=${"dot " + (p.tone || "")}
        onClick=${() => onPick?.(p.label)}><title>${p.title || p.label}</title></circle>`)}
    ${good.filter((p) => named.has(p.label)).map((p) => html`<text key=${"l" + p.label} class="dotlabel" x=${sx(p.x) + 7} y=${Math.max(m.t + 10, sy(p.y) + 4)}
        onClick=${() => onPick?.(p.label)}>${p.label}</text>`)}
    <text class="axis title" x=${(sw + m.l) / 2} y=${sh - 4} text-anchor="middle">${xLabel}</text>
    <text class="axis title" x=${12} y=${(sh - m.b) / 2} text-anchor="middle" transform=${`rotate(-90 12 ${(sh - m.b) / 2})`}>${yLabel}</text>
  </svg>${corners && html`<p class="muted small corners"><span>↖ ${corners[0]}</span><span>↘ ${corners[1]}</span></p>`}`;
}

// A number with its change against the period before.
export function Kpi({ label, now, before, note }) {
  const d = before ? (now - before) / before : null;
  return html`<div class="kpi"><span>${label}</span><b>${num(now)}</b>
    ${d != null && html`<small class=${d >= 0 ? "up" : "down"}> ${pct(d)} vs before</small>`}${note && html`<small> ${note}</small>`}</div>`;
}
