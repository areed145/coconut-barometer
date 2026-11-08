// Music → Artists (#/music/artists, music_graph.py): a force layout drawn on a canvas, run here in the browser.
// Size is plays, color is cluster. Each link pulls by what relates the two (music_graph.TYPES: bandmates close, artists
// who only sound alike loosely), and its line says which kind: thicker and darker for a stronger tie, or colored by
// kind. Cluster shapes, when on, are a soft field behind each cluster's artists. The layout keeps each artist's
// place when the range, the number drawn or the kinds shown change, or MusicBrainz fills in more links, so the network
// moves to its new shape instead of starting over. Wheel or pinch to zoom, drag the background to pan, drag an artist
// to pull it, click to see its connections, double-click to zoom to its neighbours.
import { html, useState, useEffect, useRef, useMemo, api, num, toast, every, useThemeTick } from "./lib.js";
import { Period, periodParams, periodName } from "./period.js";

const ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

// The five kinds of relationship (music_graph.py), strongest first, as their lines are drawn.
export const KINDS = {
  band: { label: "Bandmates", color: "--chart-4", dash: [], width: 3.4, alpha: 0.85 },
  together: { label: "Made music together", color: "--chart-2", dash: [], width: 2.4, alpha: 0.75 },
  credit: { label: "Worked on their records", color: "--chart-3", dash: [], width: 1.6, alpha: 0.62, arrow: true },
  shared: { label: "Share a collaborator", color: "--chart-1", dash: [5, 4], width: 1.3, alpha: 0.5 },
  alike: { label: "Sound alike", color: "--muted", dash: [1.5, 4], width: 1.1, alpha: 0.35 },
};
export const ALL_KINDS = Object.fromEntries(Object.keys(KINDS).map((k) => [k, true]));
const kindOf = (graph, r) => graph.types?.[r.type]?.kind || "alike";
export const shownRels = (graph, e, kinds) => e.rels.filter((r) => kinds[kindOf(graph, r)]);
export const reasons = (e) => e.rels.map((r) => r.said).join(" · ");

// A link as the layout uses it, from the relationships shown (strongest first, as the server sends them): the
// strongest sets the distance, each other adds 30% of its pull, up to 4. null when none of its kinds are shown.
export function tie(graph, e, kinds) {
  const rels = shownRels(graph, e, kinds);
  if (!rels.length) return null;
  const top = rels[0], type = graph.types?.[top.type] || { pull: 1, rest: 90 };
  const pull = Math.min(4, top.pull + 0.3 * rels.slice(1).reduce((n, r) => n + r.pull, 0));
  const rest = Math.min(...rels.map((r) => graph.types?.[r.type]?.rest ?? 90));
  return { pull, rest, top, kind: kindOf(graph, top), base: type.pull || 1 };
}

// Lines by weight or by kind, and cluster shapes: remembered in this browser, the same on both Artists views.
const remembered = (key, or) => { try { return localStorage.getItem(key) ?? or; } catch { return or; } };
const remember = (key, v) => { try { localStorage.setItem(key, v); } catch { /* this session only */ } };
export function useNetLook() {
  const [lines, setLines] = useState(() => remembered("net-lines", "weight"));
  const [shapes, setShapes] = useState(() => remembered("net-shapes", "off") === "on");
  return { lines, shapes, setLines: (v) => { remember("net-lines", v); setLines(v); }, setShapes: (v) => { remember("net-shapes", v ? "on" : "off"); setShapes(v); } };
}

const swatch = (k) => {
  const st = KINDS[k], c = `var(${st.color})`;
  const bg = st.dash.length ? `repeating-linear-gradient(90deg, ${c} 0 ${st.dash[0] + 1}px, transparent ${st.dash[0] + 1}px ${st.dash[0] + st.dash[1] + 1}px)` : c;
  return `height:${Math.max(2, Math.round(st.width))}px;background:${bg}`;
};

// The kinds shown, with how many links have each, then how lines are drawn and whether clusters get shapes.
export function NetOptions({ graph, kinds, setKinds, look }) {
  const counts = useMemo(() => {
    const c = {};
    for (const e of graph.edges) for (const k of new Set(e.rels.map((r) => kindOf(graph, r)))) c[k] = (c[k] || 0) + 1;
    return c;
  }, [graph]);
  return html`<div class="netkinds">${Object.entries(KINDS).map(([k, v]) => html`<label key=${k}><input type="checkbox" checked=${kinds[k]} onChange=${() => setKinds({ ...kinds, [k]: !kinds[k] })} />
      <i class="netswatch" style=${swatch(k)}></i>${v.label} <span class="muted">${num(counts[k] || 0)}</span></label>`)}</div>
    <div class="netkinds"><span class="muted">Lines</span><span class="seg" aria-label="Lines">
        <button class=${look.lines === "weight" ? "primary" : ""} aria-pressed=${look.lines === "weight"} onClick=${() => look.setLines("weight")}>Weight</button>
        <button class=${look.lines === "kind" ? "primary" : ""} aria-pressed=${look.lines === "kind"} onClick=${() => look.setLines("kind")}>Colored by kind</button></span>
      <label><input type="checkbox" checked=${look.shapes} onChange=${() => look.setShapes(!look.shapes)} /> Cluster shapes</label></div>`;
}
const SIZES = [50, 100, 150, 200];
const hue = (c, n) => Math.round((c * 360) / Math.max(n, 5) + 20) % 360;
export const pairKey = (a, b) => (a < b ? `${a}\u0000${b}` : `${b}\u0000${a}`);
export const color = (c, n) => `hsl(${hue(c, n)} 60% 52%)`;
const radius = (n, max) => 5 + 17 * Math.sqrt(n / max);
const isDark = (bg) => { const m = /^#([0-9a-f]{2})/i.exec(bg); return !!m && parseInt(m[1], 16) < 80; };
const height = () => Math.round(Math.max(420, Math.min(window.innerHeight - 240, 860)));

// ---- the simulation: positions persist by artist across new data ----------------------------------------
function rebuild(graph, old, w, h) {
  const k = Math.max(1, graph.clusters.length);
  const home = graph.clusters.map((c, i) => [w / 2 + Math.cos((i / k) * 6.283) * w * 0.3, h / 2 + Math.sin((i / k) * 6.283) * h * 0.3]);
  const max = Math.max(1, ...graph.nodes.map((n) => n.plays));
  const before = new Map((old?.nodes || []).map((n) => [n.id, n]));
  const nodes = graph.nodes.map((n) => {
    const [hx, hy] = home[n.cluster] || [w / 2, h / 2];
    const was = before.get(n.id);
    return { ...n, r: radius(n.plays, max), hx, hy, x: was?.x, y: was?.y, vx: was?.vx || 0, vy: was?.vy || 0, fresh: !was };
  });
  const index = new Map(nodes.map((n) => [n.id, n]));
  const links = graph.edges.map((e) => ({ ...e, s: index.get(e.a), t: index.get(e.b), tie: null })).filter((l) => l.s && l.t);
  nodes.forEach((n, i) => {                     // a new artist starts beside one it's linked to, else near its cluster
    if (!n.fresh) return;
    const by = links.find((l) => (l.s === n && !l.t.fresh) || (l.t === n && !l.s.fresh));
    const near = by ? (by.s === n ? by.t : by.s) : null, a = i * 2.4, spread = near ? 30 : 40 * Math.sqrt(i % 9);
    n.x = (near ? near.x : n.hx) + Math.cos(a) * spread;
    n.y = (near ? near.y : n.hy) + Math.sin(a) * spread;
  });
  return { nodes, links, index };
}

function tick(sim, alpha, w, h, pinned) {
  const { nodes, links } = sim;
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      let dx = b.x - a.x, dy = b.y - a.y;
      const d2 = dx * dx + dy * dy || 0.01;
      if (d2 > 120000) continue;
      const d = Math.sqrt(d2), min = a.r + b.r + 6, f = (1500 / d2 + (d < min ? (min - d) * 0.3 : 0)) * alpha;
      dx /= d; dy /= d;
      a.vx -= dx * f; a.vy -= dy * f; b.vx += dx * f; b.vy += dy * f;
    }
  }
  for (const l of links) {                      // only the kinds shown pull: turning one off reshapes the network
    const t = l.tie;
    if (!t) continue;
    const dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
    const f = (d - (t.rest + l.s.r + l.t.r)) * 0.016 * t.pull * alpha;
    l.s.vx += (dx / d) * f; l.s.vy += (dy / d) * f; l.t.vx -= (dx / d) * f; l.t.vy -= (dy / d) * f;
  }
  for (const n of nodes) {
    n.vx += ((n.hx - n.x) * 0.008 + (w / 2 - n.x) * 0.003) * alpha;
    n.vy += ((n.hy - n.y) * 0.008 + (h / 2 - n.y) * 0.003) * alpha;
    if (n === pinned) { n.vx = n.vy = 0; continue; }
    n.vx *= 0.8; n.vy *= 0.8;
    n.x += n.vx; n.y += n.vy;
  }
}

// ---- cluster shapes: a soft field around each member, 24px past its circle, joined along the links between
// clustermates that are short enough, so a cluster the layout splits shows as two shapes rather than one hull over
// everything between. Worked out on a grid a quarter of the screen's size
// (each cluster only over its own box), then scaled up smoothly behind the links.
const CELL = 4;
const SHAPE_JOIN = 180;                         // px of the layout: links up to this long join two members' shapes
const shapeLayer = typeof document !== "undefined" ? document.createElement("canvas") : null;
function hslRgb(h, s, l) {
  s /= 100; l /= 100;
  const f = (n) => { const k = (n + h / 30) % 12, a = s * Math.min(l, 1 - l); return Math.round(255 * (l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1)))); };
  return [f(0), f(8), f(4)];
}
const smooth = (a, b, x) => { const t = Math.max(0, Math.min(1, (x - a) / (b - a))); return t * t * (3 - 2 * t); };
function drawShapes(ctx, s, clusters) {
  const W = Math.ceil(s.w / CELL), H = Math.ceil(s.h / CELL), v = s.view;
  if (shapeLayer.width !== W || shapeLayer.height !== H) { shapeLayer.width = W; shapeLayer.height = H; }
  if (s.shapeBest?.length !== W * H) { s.shapeBest = new Float32Array(W * H); s.shapeOf = new Int32Array(W * H); }
  const best = s.shapeBest, of = s.shapeOf;
  best.fill(0); of.fill(-1);
  const groups = new Map();
  for (const n of s.sim.nodes) { if (!groups.has(n.cluster)) groups.set(n.cluster, []); groups.get(n.cluster).push(n); }
  s.joins = new Map();
  for (const l of s.sim.links) {
    if (!l.tie || l.s.cluster !== l.t.cluster || Math.hypot(l.t.x - l.s.x, l.t.y - l.s.y) > SHAPE_JOIN) continue;
    if (!s.joins.has(l.s.cluster)) s.joins.set(l.s.cluster, []);
    s.joins.get(l.s.cluster).push([l.s, l.t]);
  }
  for (const [c, members] of groups) {
    if (members.length < 2) continue;
    const at = (n) => [(n.x * v.k + v.x) / CELL, (n.y * v.k + v.y) / CELL];
    const dots = members.map((n) => [...at(n), ((n.r + 24) * v.k) / CELL]);
    const joins = (s.joins.get(c) || []).map(([a, b]) => [...at(a), ...at(b), ((Math.min(a.r, b.r) + 16) * v.k) / CELL]);
    const x0 = Math.max(0, Math.floor(Math.min(...dots.map((d) => d[0] - d[2])))), x1 = Math.min(W - 1, Math.ceil(Math.max(...dots.map((d) => d[0] + d[2]))));
    const y0 = Math.max(0, Math.floor(Math.min(...dots.map((d) => d[1] - d[2])))), y1 = Math.min(H - 1, Math.ceil(Math.max(...dots.map((d) => d[1] + d[2]))));
    if (x1 < x0 || y1 < y0) continue;
    const bw = x1 - x0 + 1, field = new Float32Array(bw * (y1 - y0 + 1));
    for (const [sx, sy, R] of dots) {
      const R2 = R * R;
      for (let y = Math.max(y0, Math.floor(sy - R)); y <= Math.min(y1, Math.ceil(sy + R)); y++) {
        for (let x = Math.max(x0, Math.floor(sx - R)); x <= Math.min(x1, Math.ceil(sx + R)); x++) {
          const d2 = (x - sx) ** 2 + (y - sy) ** 2;
          if (d2 < R2) { const q = 1 - d2 / R2; field[(y - y0) * bw + x - x0] += q * q; }
        }
      }
    }
    for (const [ax, ay, bx, by, R] of joins) {  // a capsule along the link: the distance to the segment, not a point
      const R2 = R * R, dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy || 1;
      for (let y = Math.max(y0, Math.floor(Math.min(ay, by) - R)); y <= Math.min(y1, Math.ceil(Math.max(ay, by) + R)); y++) {
        for (let x = Math.max(x0, Math.floor(Math.min(ax, bx) - R)); x <= Math.min(x1, Math.ceil(Math.max(ax, bx) + R)); x++) {
          const t = Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / len2));
          const d2 = (x - ax - t * dx) ** 2 + (y - ay - t * dy) ** 2;
          if (d2 < R2) { const q = 1 - d2 / R2; field[(y - y0) * bw + x - x0] += q * q; }
        }
      }
    }
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
      const d = field[(y - y0) * bw + x - x0], i = y * W + x;
      if (d > best[i]) { best[i] = d; of[i] = c; }
    }
  }
  const img = new ImageData(W, H), px = img.data, rgb = new Map();
  for (let i = 0; i < W * H; i++) {
    const d = best[i];
    if (d < 0.16) continue;
    const c = of[i];
    if (!rgb.has(c)) rgb.set(c, hslRgb(hue(c, clusters), 60, 52));
    const inside = smooth(0.16, 0.26, d), rim = inside * (1 - smooth(0.26, 0.42, d)), col = rgb.get(c), o = i * 4;
    px[o] = col[0]; px[o + 1] = col[1]; px[o + 2] = col[2]; px[o + 3] = Math.round((0.13 * inside + 0.17 * rim) * 255);
  }
  shapeLayer.getContext("2d").putImageData(img, 0, 0);
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(shapeLayer, 0, 0, W * CELL, H * CELL);
}

function bounds(nodes) {
  if (!nodes.length) return null;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of nodes) { x0 = Math.min(x0, n.x - n.r); y0 = Math.min(y0, n.y - n.r); x1 = Math.max(x1, n.x + n.r); y1 = Math.max(y1, n.y + n.r + 14); }
  return { x0, y0, x1, y1 };
}

// highlight: { nodes: Set, edges: Set of pairKey } to light up (a path, what a filter found), dimming the rest.
// fitOn: a new value fits everything in view, once new artists have found their places.
// lines: "weight" (darker and thicker for a stronger tie) or "kind" (each kind its color). shapes: clusters as shapes.
export function Network({ graph, kinds, selected, setSelected, cluster, focusOn, highlight, fitOn, lines = "weight", shapes = false }) {
  const box = useRef(null), canvas = useRef(null), tip = useRef(null), s = useRef({}).current;
  const theme = useThemeTick();
  const [h, setH] = useState(height);

  // The loop: steps the forces while they settle, eases the view to where it's going, marches the selected artist's
  // links, and stops when nothing moves.
  useEffect(() => {
    Object.assign(s, { alpha: 1, view: { k: 1, x: 0, y: 0 }, goal: null, pointers: new Map(), hover: null, dash: 0 });
    const run = (t) => {
      s.frame = null;
      if (s.sim && s.alpha > 0.02) { s.alpha *= 0.985; tick(s.sim, s.alpha, s.w, s.h, s.pinned); }
      if (s.goal) {
        const v = s.view, g = s.goal;
        v.k += (g.k - v.k) * 0.18; v.x += (g.x - v.x) * 0.18; v.y += (g.y - v.y) * 0.18;
        if (Math.abs(g.k - v.k) < 0.002 && Math.abs(g.x - v.x) < 0.5 && Math.abs(g.y - v.y) < 0.5) { Object.assign(v, g); s.goal = null; }
      }
      if (s.autofit && s.alpha < s.autofit) { s.autofit = 0; s.fit(); }   // autofit: fit once the forces cool to it
      s.dash = (t / 60) % 20;
      s.draw?.();
      if (s.alpha > 0.02 || s.goal || s.pinned || s.selected || s.autofit) s.frame = requestAnimationFrame(run);
    };
    s.wake = (heat = 0) => { s.alpha = Math.max(s.alpha, heat); if (!s.frame) s.frame = requestAnimationFrame(run); };
    s.fit = (nodes) => {
      const b = bounds(nodes || s.sim?.nodes || []);
      if (!b) return;
      const pad = 24, k = Math.min(2.5, Math.max(0.2, Math.min((s.w - 2 * pad) / (b.x1 - b.x0 || 1), (s.h - 2 * pad) / (b.y1 - b.y0 || 1))));
      s.goal = { k, x: s.w / 2 - ((b.x0 + b.x1) / 2) * k, y: s.h / 2 - ((b.y0 + b.y1) / 2) * k };
      s.wake();
    };
    s.zoom = (factor, cx = s.w / 2, cy = s.h / 2) => {
      const v = s.goal || s.view, k = Math.min(6, Math.max(0.15, v.k * factor));
      s.goal = { k, x: cx - ((cx - v.x) / v.k) * k, y: cy - ((cy - v.y) / v.k) * k };
      s.wake();
    };
    return () => { cancelAnimationFrame(s.frame); s.frame = null; };
  }, []);

  // The canvas follows its box's width and the window's height.
  useEffect(() => {
    const el = canvas.current, size = () => {
      const w = box.current.clientWidth, dpr = window.devicePixelRatio || 1, hh = height();
      if (w === s.w && hh === s.h) return;
      Object.assign(s, { w, h: hh, dpr });
      el.width = w * dpr; el.height = hh * dpr;
      setH(hh); s.wake();
    };
    size();
    const watch = new ResizeObserver(size);
    watch.observe(box.current);
    window.addEventListener("resize", size);
    return () => { watch.disconnect(); window.removeEventListener("resize", size); };
  }, []);

  // Each link's pull and distance from the kinds shown, the weakest drawn first so the strong lie on top.
  const retie = () => {
    for (const l of s.sim.links) l.tie = tie(graph, l, s.kinds);
    s.sim.links.sort((x, y) => (x.tie?.pull || 0) - (y.tie?.pull || 0));
  };
  useEffect(() => {
    const first = !s.sim;
    s.kinds = s.kinds || kinds;
    s.sim = rebuild(graph, s.sim, s.w, s.h);
    retie();
    if (first) s.autofit = 0.5;
    s.wake(first ? 1 : 0.5);
  }, [graph]);
  useEffect(() => { s.kinds = kinds; if (s.sim) retie(); s.wake(0.35); }, [kinds]);
  useEffect(() => { s.lines = lines; s.shapes = shapes; s.wake(); }, [lines, shapes]);
  useEffect(() => { s.selected = selected; s.cluster = cluster; s.wake(); }, [selected, cluster]);
  useEffect(() => {                             // the search, a bridge or a link in the detail: zoom to it
    const n = focusOn && s.sim?.index.get(focusOn.id);
    if (!n) return;
    const k = Math.max(1.4, (s.goal || s.view).k);
    s.goal = { k, x: s.w / 2 - n.x * k, y: s.h / 2 - n.y * k };
    s.wake();
  }, [focusOn]);
  useEffect(() => {                             // a cluster picked in the list: fit it
    if (cluster != null && s.sim) s.fit(s.sim.nodes.filter((n) => n.cluster === cluster));
  }, [cluster]);
  useEffect(() => {                             // new artists joined: fit everything once they've settled
    if (!fitOn || !s.sim) return;
    s.autofit = 0.12;
    s.wake(0.5);
  }, [fitOn]);
  useEffect(() => {                             // something lit up: fit it, once the new artists have found their places
    s.highlight = highlight;
    if (highlight && s.sim) setTimeout(() => s.fit(s.sim.nodes.filter((n) => highlight.nodes.has(n.id))), 400);
    s.wake();
  }, [highlight]);

  useEffect(() => {
    const el = canvas.current;
    s.draw = () => {
      if (!s.sim || !s.w) return;
      const ctx = el.getContext("2d"), css = getComputedStyle(el), v = s.view, k = s.kinds || kinds;
      const value = (name, or) => css.getPropertyValue(name).trim() || or;
      const fg = value("--fg", "#222"), line = value("--line", "#ccc"), clusters = graph.clusters.length;
      ctx.setTransform(s.dpr, 0, 0, s.dpr, 0, 0);
      ctx.clearRect(0, 0, s.w, s.h);
      ctx.setTransform(s.dpr * v.k, 0, 0, s.dpr * v.k, s.dpr * v.x, s.dpr * v.y);
      const focus = s.selected || s.hover?.id;
      const near = new Set(focus ? [focus] : []);
      if (focus) for (const l of s.sim.links) if ((l.a === focus || l.b === focus) && l.tie) { near.add(l.a); near.add(l.b); }
      const inCluster = (n) => s.cluster == null || n.cluster === s.cluster;
      const hl = s.highlight, lit = (l) => !!hl && hl.edges.has(pairKey(l.a, l.b));
      if (s.shapes) {                           // behind everything, in screen space
        ctx.setTransform(s.dpr, 0, 0, s.dpr, 0, 0);
        drawShapes(ctx, s, clusters);
        ctx.setTransform(s.dpr * v.k, 0, 0, s.dpr * v.k, s.dpr * v.x, s.dpr * v.y);
      }
      const byKind = s.lines === "kind";
      for (const l of s.sim.links) {
        const t = l.tie || (lit(l) ? tie(graph, l, ALL_KINDS) : null);   // what an answer lit up shows, whatever's hidden
        if (!t) continue;
        const mine = focus && (l.a === focus || l.b === focus), on = focus ? mine : hl ? lit(l) : inCluster(l.s) && inCluster(l.t);
        const style = KINDS[t.kind], marching = mine && s.selected, ink = byKind ? value(style.color, fg) : fg;
        ctx.setLineDash((marching && !style.dash.length ? [6, 4] : style.dash).map((d) => d / v.k));
        ctx.lineDashOffset = marching ? -s.dash / v.k : 0;
        ctx.lineWidth = (style.width * (0.75 + Math.min(1.6, t.pull / t.base) * 0.25) * (!focus && lit(l) ? 1.8 : 1)) / Math.sqrt(v.k);
        ctx.strokeStyle = ink;
        ctx.globalAlpha = on ? (focus ? 0.95 : hl ? 0.7 : style.alpha * (byKind ? 1 : 0.8)) : 0.08;
        ctx.beginPath(); ctx.moveTo(l.s.x, l.s.y); ctx.lineTo(l.t.x, l.t.y); ctx.stroke();
        if (style.arrow && t.top.from) {        // from the one who did the work
          const from = t.top.from === l.a ? l.s : l.t, to = from === l.s ? l.t : l.s;
          const dx = to.x - from.x, dy = to.y - from.y, d = Math.hypot(dx, dy) || 1, ux = dx / d, uy = dy / d;
          const tx = to.x - ux * (to.r + 3), ty = to.y - uy * (to.r + 3), len = 7 / Math.sqrt(v.k), half = 3.5 / Math.sqrt(v.k);
          ctx.fillStyle = ink;
          ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(tx - ux * len - uy * half, ty - uy * len + ux * half);
          ctx.lineTo(tx - ux * len + uy * half, ty - uy * len - ux * half); ctx.closePath(); ctx.fill();
        }
      }
      ctx.setLineDash([]); ctx.lineDashOffset = 0;
      ctx.font = `${12 / v.k}px 'DM Sans', sans-serif`; ctx.textAlign = "center";
      for (const n of s.sim.nodes) {
        const on = focus ? near.has(n.id) : hl ? hl.nodes.has(n.id) : inCluster(n);
        ctx.globalAlpha = on ? 1 : 0.15;
        ctx.fillStyle = color(n.cluster, clusters);
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, 6.283); ctx.fill();
        if (n.id === s.selected || n === s.hover) { ctx.strokeStyle = fg; ctx.lineWidth = 2 / v.k; ctx.stroke(); }
        if (on && (n.r * v.k > 11 || n.id === s.selected || n === s.hover || (focus && near.has(n.id)) || (!focus && hl?.nodes.has(n.id)) || (s.cluster != null && v.k > 1.2))) {
          ctx.fillStyle = fg; ctx.fillText(n.id.length > 24 ? `${n.id.slice(0, 23)}…` : n.id, n.x, n.y + n.r + 13 / v.k);
        }
      }
      if (s.shapes && !focus && !hl) {          // each shape's cluster name, above its artists
        const groups = new Map();
        for (const n of s.sim.nodes) { if (!groups.has(n.cluster)) groups.set(n.cluster, []); groups.get(n.cluster).push(n); }
        const dark = isDark(value("--surface-2", "#eee"));
        ctx.font = `700 ${12 / v.k}px 'DM Sans', sans-serif`;
        for (const [c, members] of groups) {
          const label = graph.clusters[c]?.label;
          if (members.length < 2 || !label || !members.some(inCluster)) continue;
          const x = members.reduce((n, m) => n + m.x, 0) / members.length, y = Math.min(...members.map((m) => m.y - m.r)) - 16 / v.k;
          ctx.globalAlpha = 0.9; ctx.lineWidth = 4 / v.k; ctx.strokeStyle = value("--surface-2", "#eee"); ctx.strokeText(label.toUpperCase(), x, y);
          ctx.fillStyle = `hsl(${hue(c, clusters)} 55% ${dark ? 68 : 38}%)`; ctx.fillText(label.toUpperCase(), x, y);
        }
      }
      ctx.globalAlpha = 1;
    };
    s.draw();
  }, [graph, kinds, theme, lines, shapes]);

  // ---- pointers: drag an artist, pan, pinch, hover ----
  const world = (e) => {
    const b = canvas.current.getBoundingClientRect(), v = s.view, sx = e.clientX - b.left, sy = e.clientY - b.top;
    return { sx, sy, x: (sx - v.x) / v.k, y: (sy - v.y) / v.k };
  };
  const hit = (p) => s.sim && [...s.sim.nodes].reverse().find((n) => (n.x - p.x) ** 2 + (n.y - p.y) ** 2 <= (n.r + 4 / s.view.k) ** 2);
  const showTip = (n, p) => {
    const el = tip.current;
    if (!n) { el.hidden = true; return; }
    const links = s.sim.links.filter((l) => (l.a === n.id || l.b === n.id) && l.tie).length;
    const c = graph.clusters[n.cluster];
    el.textContent = `${n.id} · ${num(n.plays)} plays · ${links} link${links === 1 ? "" : "s"}${c ? ` · ${c.label}` : ""}`;
    el.hidden = false;
    el.style.left = `${Math.max(4, Math.min(p.sx + 14, s.w - el.offsetWidth - 4))}px`; el.style.top = `${Math.max(4, p.sy - 34)}px`;
  };
  const down = (e) => {
    const p = world(e);
    canvas.current.setPointerCapture(e.pointerId);
    s.pointers.set(e.pointerId, p);
    s.moved = false; s.goal = null;
    if (s.pointers.size === 2) {
      const [a, b] = [...s.pointers.values()];
      s.pinch = { d: Math.hypot(a.sx - b.sx, a.sy - b.sy) || 1, k: s.view.k };
      s.pinned = null; s.pan = null;
      return;
    }
    const n = hit(p);
    if (n) { s.pinned = n; s.wake(0.3); } else s.pan = { sx: p.sx, sy: p.sy, x: s.view.x, y: s.view.y };
  };
  const move = (e) => {
    const p = world(e);
    if (s.pointers.has(e.pointerId)) s.pointers.set(e.pointerId, p);
    if (s.pinch && s.pointers.size === 2) {
      const [a, b] = [...s.pointers.values()], d = Math.hypot(a.sx - b.sx, a.sy - b.sy);
      const k = Math.min(6, Math.max(0.15, s.pinch.k * (d / s.pinch.d))), cx = (a.sx + b.sx) / 2, cy = (a.sy + b.sy) / 2, v = s.view;
      Object.assign(v, { x: cx - ((cx - v.x) / v.k) * k, y: cy - ((cy - v.y) / v.k) * k, k });
      s.moved = true; s.wake(); return;
    }
    if (s.pinned) { s.pinned.x = p.x; s.pinned.y = p.y; s.moved = true; showTip(null); s.wake(0.3); return; }
    if (s.pan) {
      if (Math.abs(p.sx - s.pan.sx) + Math.abs(p.sy - s.pan.sy) > 3) s.moved = true;
      s.view.x = s.pan.x + p.sx - s.pan.sx; s.view.y = s.pan.y + p.sy - s.pan.sy; s.wake(); return;
    }
    const n = hit(p);
    if (n !== s.hover) { s.hover = n; canvas.current.style.cursor = n ? "pointer" : "grab"; s.wake(); }
    showTip(n, p);
  };
  const up = (e) => {
    s.pointers.delete(e.pointerId);
    if (s.pinch) { if (!s.pointers.size) s.pinch = null; return; }
    if (s.pinned && !s.moved) setSelected(s.pinned.id === s.selected ? null : s.pinned.id);
    else if (s.pan && !s.moved) setSelected(null);
    s.pinned = null; s.pan = null;
  };
  const dbl = (e) => {
    const n = hit(world(e));
    if (!n) { s.fit(); return; }
    const ids = new Set([n.id]);
    for (const l of s.sim.links) if (l.a === n.id || l.b === n.id) { ids.add(l.a); ids.add(l.b); }
    setSelected(n.id);
    s.fit(s.sim.nodes.filter((m) => ids.has(m.id)));
  };
  useEffect(() => {                             // not passive, so the page doesn't scroll while the network zooms
    const el = canvas.current, wheel = (e) => { e.preventDefault(); const p = world(e); s.zoom(Math.exp(-e.deltaY * 0.0015), p.sx, p.sy); };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, []);
  const leave = () => { s.hover = null; showTip(null); s.wake(); };
  return html`<div class="netbox" ref=${box}>
    <canvas ref=${canvas} class="netgraph" role="img" aria-label="Network of the artists you listen to" style=${`height:${h}px`}
      onPointerDown=${down} onPointerMove=${move} onPointerUp=${up} onPointerCancel=${up} onPointerLeave=${leave} onDblClick=${dbl}></canvas>
    <div class="nettip" ref=${tip} hidden></div>
    <div class="netzoom"><button aria-label="Zoom in" onClick=${() => s.zoom(1.4)}>+</button><button aria-label="Zoom out" onClick=${() => s.zoom(1 / 1.4)}>−</button>
      <button onClick=${() => s.fit()}>Fit</button><button title="Shake the layout loose" onClick=${() => s.wake(0.8)}>Shake</button></div>
  </div>`;
}

// An artist's profile in a line: scenes, years, place, genres.
export const profileLine = (p) => p && [p.scenes.join(", "), p.era, [p.city, p.country].filter(Boolean).join(", "),
  p.genres.slice(0, 3).join(", ")].filter(Boolean).join(" · ");

export function Detail({ graph, id, kinds, go, extra }) {
  const node = graph.nodes.find((n) => n.id === id);
  const cluster = graph.clusters[node.cluster];
  const rows = graph.edges.map((e) => ({ e, t: (e.a === id || e.b === id) && tie(graph, e, kinds) })).filter((r) => r.t)
    .map(({ e, t }) => ({ other: e.a === id ? e.b : e.a, rels: shownRels(graph, e, kinds), pull: t.pull }))
    .sort((a, b) => b.pull - a.pull);
  return html`<div class="netdetail"><h3>${id}</h3>
    <p class="muted small">${num(node.plays)} plays · ${num(node.minutes)} minutes${node.genre ? ` · ${node.genre}` : ""}${cluster ? ` · ${cluster.label}` : ""}</p>
    ${profileLine(node.profile) && html`<p class="muted small">${profileLine(node.profile)}</p>`}
    ${extra}
    ${rows.length ? html`<ul>${rows.map((r) => html`<li key=${r.other}><button class="link" onClick=${() => go(r.other)}>${r.other}</button>
        ${r.rels.map((x) => html`<span class="netwhy" key=${x.type + x.said}><i class="netdot" style=${`background:var(${KINDS[kindOf(graph, x)].color})`}></i>
          <span class="muted">${x.said}</span>${x.via.map((v) => html`<span class="netvia" key=${v}>${v}</span>`)}</span>`)}</li>`)}</ul>`
      : html`<p class="muted small">No connections shown.</p>`}</div>`;
}

function MusicBrainzNote({ mb, onAsk, asking }) {
  if (!mb) return null;
  if (!mb.enabled) return html`<p class="muted small">Turn on MusicBrainz in <a href="#/settings/music">Settings → Music</a> to link artists by producers, writers, players and band members.</p>`;
  if (!mb.ready) return html`<p class="muted small">Add a contact for MusicBrainz in <a href="#/settings/music">Settings → Music</a> to start looking up credits.</p>`;
  const left = mb.tracks - mb.asked;
  return html`<p class="muted small">MusicBrainz: ${num(mb.asked)} of ${num(mb.tracks)} tracks looked up (${num(mb.found)} found), ${num(mb.artists)} artists.
    ${left > 0 ? " Lookups run as jobs at one request a second, and the network fills in as they finish." : ""}
    ${left > 0 && !mb.local && html` <button class="link" disabled=${asking} onClick=${onAsk}>Look up more now</button>`}</p>`;
}

function ProfilesNote({ p, onAsk, onReset, asking }) {
  if (!p) return null;
  if (!p.enabled) return html`<p class="muted small">Turn on artist profiles in <a href="#/settings/music">Settings → Music</a> to link artists by scene, era and sound.</p>`;
  if (!p.can_ask) return html`<p class="muted small">Artist profiles need the Anthropic key in <a href="#/settings/vision">Settings → Vision</a>.</p>`;
  const left = p.eligible - p.asked;
  return html`<p class="muted small">Artist profiles: ${num(p.asked)} of your ${num(p.eligible)} artists with over ${num(p.min_plays)} plays, ${num(p.calls_today)} of ${num(p.daily_calls)} requests today.
    ${left > 0 ? " They run as jobs of 25 artists, and profile matches fill in as they finish." : ""}
    ${left > 0 && !p.local && p.calls_today < p.daily_calls && html` <button class="link" disabled=${asking} onClick=${onAsk}>Profile more now</button>`}
    ${p.asked > 0 && html` <button class="link" onClick=${onReset}>Start over</button>`}</p>`;
}

// MusicBrainz's and the profiles' progress, and buttons to hurry them along: below both Artists views, since both draw
// the links they add. Checked again every 2 minutes while either is still filling in.
export function LinkData() {
  const [st, setSt] = useState(null);
  const [asking, setAsking] = useState(false);
  const [looking, setLooking] = useState(false);
  const load = () => api("/api/music/graph/state").then(setSt).catch(() => {});
  const filling = !!(st?.musicbrainz?.ready && st.musicbrainz.asked < st.musicbrainz.tracks)
    || !!(st?.profiles?.ready && st.profiles.asked < st.profiles.eligible && st.profiles.calls_today < st.profiles.daily_calls);
  useEffect(() => (filling ? every(load, 120000) : (load(), undefined)), [filling]);
  const queue = async (path, setBusy, busyText, missing) => {
    setBusy(true);
    try {
      const r = await api(path, { method: "POST" });
      toast(r.status === "queued" ? "Queued: it runs after the jobs ahead of it (Status → Jobs)" : r.status === "busy" ? busyText : missing);
    } catch (e) { toast(e.message); }
    setBusy(false);
  };
  const reset = async () => {
    if (!confirm("Forget every artist profile? They're asked for again, which uses requests.")) return;
    try { await api("/api/music/graph/profiles", { method: "DELETE" }); load(); } catch (e) { toast(e.message); }
  };
  if (!st) return null;
  return html`<div class="card"><h2>Where the links come from</h2>
    <${MusicBrainzNote} mb=${st.musicbrainz} asking=${looking}
      onAsk=${() => queue("/api/music/graph/musicbrainz", setLooking, "A lookup is already waiting or running", "MusicBrainz isn't set up")} />
    <${ProfilesNote} p=${st.profiles} asking=${asking} onReset=${reset}
      onAsk=${() => queue("/api/music/graph/profiles", setAsking, "Profiles are already waiting or running", "Artist profiles aren't set up")} /></div>`;
}

// Music → Artists, "Top in this period": the most played artists of the shared period and how they connect.
// lead: what goes first in the toolbar (the view switch).
export function ArtistNetwork({ focus, period, setPeriod, lead }) {
  const [limit, setLimit] = useState(100);
  const [g, setG] = useState(null);
  const [error, setError] = useState(null);
  const [kinds, setKinds] = useState(ALL_KINDS);
  const look = useNetLook();
  const [selected, setSelected] = useState(null);
  const [cluster, setCluster] = useState(null);
  const [focusOn, setFocusOn] = useState(null);
  const [query, setQuery] = useState("");
  const go = (id) => { setSelected(id); setCluster(null); setFocusOn({ id, at: Date.now() }); };
  const load = () => api("/api/music/graph", { params: { ...periodParams(period), limit, tz: ZONE } }).then((x) => { setG(x); setError(null); }).catch((e) => setError(e.message));
  useEffect(() => { load(); }, [period, limit]);
  const filling = !!(g?.musicbrainz?.ready && g.musicbrainz.asked < g.musicbrainz.tracks)
    || !!(g?.profiles?.ready && g.profiles.asked < g.profiles.eligible && g.profiles.calls_today < g.profiles.daily_calls);
  useEffect(() => (filling ? every(load, 120000) : undefined), [filling, period, limit]);   // links arrive as the jobs finish
  useEffect(() => { if (focus && g?.nodes.some((n) => n.id === focus)) go(focus); }, [focus, !!g]);
  const search = (e) => {
    e.preventDefault();
    const q = query.trim().toLowerCase();
    const hit = q && (g.nodes.find((n) => n.id.toLowerCase() === q) || g.nodes.find((n) => n.id.toLowerCase().includes(q)));
    if (hit) go(hit.id); else toast("Not among the artists drawn");
  };
  const toolbar = html`<div class="toolbar">${lead}<${Period} value=${period} onChange=${setPeriod} />
    <span class="seg" aria-label="Artists drawn">${SIZES.map((n) => html`<button key=${n} class=${limit === n ? "primary" : ""} onClick=${() => setLimit(n)}>${n}</button>`)}</span>
    ${g?.nodes?.length > 0 && html`<form class="netsearch" onSubmit=${search}><input type="search" list="net-artists" placeholder="Find an artist" aria-label="Find an artist"
        value=${query} onInput=${(e) => setQuery(e.target.value)} /><datalist id="net-artists">${g.nodes.map((n) => html`<option key=${n.id} value=${n.id} />`)}</datalist></form>`}</div>`;
  if (error) return html`${toolbar}<p class="problem">${error}</p>`;
  if (!g) return html`${toolbar}<div class="boot">Loading…</div>`;
  if (g.nodes.length < 3) return html`${toolbar}<div class="card"><h2>Artist network</h2><p class="muted">Not enough listening in ${periodName(period)}: pick a longer period, or the whole graph.</p></div>`;
  return html`${toolbar}
    <div class="card"><h2>Artist network, ${periodName(period)}</h2>
      <p class="muted small">${num(g.nodes.length)} of ${num(g.total_artists)} artists, sized by plays and colored by cluster. Scroll or pinch to zoom, drag to pan or pull an artist,
        click for its connections, double-click to zoom to its neighbours.</p>
      <${NetOptions} graph=${g} kinds=${kinds} setKinds=${setKinds} look=${look} />
      <${Network} graph=${g} kinds=${kinds} selected=${selected} setSelected=${setSelected} cluster=${cluster} focusOn=${focusOn} lines=${look.lines} shapes=${look.shapes} />
      ${selected && g.nodes.some((n) => n.id === selected) && html`<${Detail} graph=${g} id=${selected} kinds=${kinds} go=${go}
        extra=${html`<p class="small"><a href=${`#/music/artists?view=whole&artist=${encodeURIComponent(selected)}`}>Explore from here in the whole graph</a></p>`} />`}</div>
    <div class="cols"><div class="card"><h2>Clusters</h2><ul class="netlist">${g.clusters.filter((c) => c.size > 1).map((c) => html`<li key=${c.id}>
        <button class="link" aria-pressed=${cluster === c.id} onClick=${() => { setSelected(null); setCluster(cluster === c.id ? null : c.id); }}>
          <i style=${`background:${color(c.id, g.clusters.length)}`}></i>${c.label}</button>
        <span class="muted"> ${c.size}: ${c.members.slice(0, 5).join(", ")}${c.size > 5 ? "…" : ""}</span></li>`)}</ul>
        ${cluster != null && html`<button class="link" onClick=${() => setCluster(null)}>Show every cluster</button>`}</div>
      <div class="card"><h2>Hidden bridges</h2>${g.bridges.length ? html`<ul class="netlist">${g.bridges.map((b) => html`<li key=${b.id}><button class="link" onClick=${() => go(b.id)}>${b.id}</button>
        <span class="muted"> ${num(b.plays)} plays, ties ${b.links.join(" ↔ ")}</span></li>`)}</ul>` : html`<p class="muted small">None yet.</p>`}</div></div>`;
}
