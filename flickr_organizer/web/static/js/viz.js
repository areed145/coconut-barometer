// Small charts for the Analytics tabs, drawn as plain HTML/SVG (no library): bar lists,
// photo tiles, a week heatmap, a word cloud and a scatter plot. Nothing uses inline
// HTML or style attributes in markup, so the Content-Security-Policy holds.
import { html, useState, useMemo, num, pick } from "./lib.js";
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
export function Bars({ rows, value = "views", right, title, onPick, empty = "No data yet." }) {
  if (!rows?.length) return html`<p class="muted">${empty}</p>`;
  const max = Math.max(...rows.map((r) => r[value] || 0), 1e-9);
  return html`<div class="bars">${rows.map((r) => html`<div class=${"bar" + (onPick ? " pick" : "")} key=${r.label}
      title=${title ? title(r) : ""} onClick=${onPick ? () => onPick(r) : undefined}>
    <span class="label">${r.label}</span>
    <span class="track"><span class="fill" style=${`width:${Math.max(0, (100 * (r[value] || 0)) / max)}%`}></span></span>
    <span class="n">${right ? right(r) : num(r[value])}</span></div>`)}</div>`;
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

// ---- word cloud ----------------------------------------------------------------------------
const W = 900, H = 440;
let measurer = null;
function measure(text, size) {
  measurer = measurer || document.createElement("canvas").getContext("2d");
  measurer.font = `600 ${size}px system-ui, -apple-system, Segoe UI, sans-serif`;
  return measurer.measureText(text).width;
}

// words: [{text, weight, tone: "up"|"down"|"", title}], biggest first. Laid out on an
// elliptical spiral, each word placed where it overlaps nothing placed before it.
function layout(words) {
  const top = Math.max(...words.map((w) => w.weight), 1e-9);
  const low = Math.min(...words.map((w) => w.weight));
  const placed = [];
  for (const w of words) {
    const t = top === low ? 1 : Math.sqrt((w.weight - low) / (top - low));
    const size = Math.round(13 + 43 * t);
    const bw = measure(w.text, size) + 6, bh = size * 0.95;
    for (let a = 0; a < 900; a += 0.33) {
      const r = 2.2 * a;
      const x = W / 2 + r * Math.cos(a) * 1.7, y = H / 2 + r * Math.sin(a);
      const box = { x0: x - bw / 2, x1: x + bw / 2, y0: y - bh / 2, y1: y + bh / 2 };
      if (box.x0 < 2 || box.x1 > W - 2 || box.y0 < 2 || box.y1 > H - 2) continue;
      if (placed.some((p) => box.x0 < p.box.x1 && box.x1 > p.box.x0 && box.y0 < p.box.y1 && box.y1 > p.box.y0)) continue;
      placed.push({ ...w, x, y, size, box });
      break;
    }
  }
  return placed;
}

export function WordCloud({ words, onPick }) {
  const placed = useMemo(() => (words.length ? layout(words) : []), [words]);
  if (!words.length) return html`<p class="muted">No tags match.</p>`;
  return html`<svg class="cloud" viewBox=${`0 0 ${W} ${H}`} role="img" aria-label="Tag cloud">
    ${placed.map((w) => html`<text key=${w.text} x=${w.x} y=${w.y} font-size=${w.size} text-anchor="middle"
        dominant-baseline="central" class=${"word " + (w.tone || "")} onClick=${() => onPick?.(w.text)}>
      <title>${w.title}</title>${w.text}</text>`)}
  </svg>`;
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
  const named = new Set([...good].sort((a, b) => b.y / b.x - a.y / a.x).slice(0, labels).map((p) => p.label));
  return html`<svg class="scatter" viewBox=${`0 0 ${sw} ${sh}`} role="img" aria-label=${`${yLabel} by ${xLabel}`}>
    ${ticks(x0, x1).filter((v) => v >= 10 ** x0 / 1.01 && v <= 10 ** x1 * 1.01).map((v) => html`<g key=${"x" + v}>
      <line class="grid" x1=${sx(v)} x2=${sx(v)} y1=${m.t} y2=${sh - m.b} /><text class="axis" x=${sx(v)} y=${sh - m.b + 16} text-anchor="middle">${num(v)}</text></g>`)}
    ${ticks(y0, y1).filter((v) => v >= 10 ** y0 / 1.01 && v <= 10 ** y1 * 1.01).map((v) => html`<g key=${"y" + v}>
      <line class="grid" x1=${m.l} x2=${sw - m.r} y1=${sy(v)} y2=${sy(v)} /><text class="axis" x=${m.l - 6} y=${sy(v) + 4} text-anchor="end">${num(v)}</text></g>`)}
    <line class="median" x1=${sx(mx)} x2=${sx(mx)} y1=${m.t} y2=${sh - m.b} />
    <line class="median" x1=${m.l} x2=${sw - m.r} y1=${sy(my)} y2=${sy(my)} />
    ${corners && html`<text class="corner" x=${m.l + 6} y=${m.t + 14}>${corners[0]}</text>
      <text class="corner" x=${sw - m.r - 6} y=${sh - m.b - 8} text-anchor="end">${corners[1]}</text>`}
    ${good.map((p) => html`<circle key=${p.label} cx=${sx(p.x)} cy=${sy(p.y)} r=${p.r || 4} class=${"dot " + (p.tone || "")}
        onClick=${() => onPick?.(p.label)}><title>${p.title || p.label}</title></circle>`)}
    ${good.filter((p) => named.has(p.label)).map((p) => html`<text key=${"l" + p.label} class="dotlabel" x=${sx(p.x) + 7} y=${sy(p.y) + 4}
        onClick=${() => onPick?.(p.label)}>${p.label}</text>`)}
    <text class="axis title" x=${(sw + m.l) / 2} y=${sh - 4} text-anchor="middle">${xLabel}</text>
    <text class="axis title" x=${12} y=${(sh - m.b) / 2} text-anchor="middle" transform=${`rotate(-90 12 ${(sh - m.b) / 2})`}>${yLabel}</text>
  </svg>`;
}

// A number with its change against the period before.
export function Kpi({ label, now, before, note }) {
  const d = before ? (now - before) / before : null;
  return html`<div class="kpi"><span>${label}</span><b>${num(now)}</b>
    ${d != null && html`<small class=${d >= 0 ? "up" : "down"}> ${pct(d)} vs before</small>`}${note && html`<small> ${note}</small>`}</div>`;
}
