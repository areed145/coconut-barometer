// The artist network on Music (music_graph.py): a force layout drawn on a canvas, run here in the browser. Size is plays,
// color is cluster; the line style says why two artists are linked.
import { html, useState, useEffect, useRef, useMemo, api, num, toast, useThemeTick } from "./lib.js";

const KINDS = {
  colisten: { label: "Played together", dash: [], width: 1 },
  collab: { label: "Collaborations", dash: [], width: 3 },
  credit: { label: "Credits", dash: [7, 4], width: 1.5 },
  similar: { label: "AI: similar", dash: [2, 4], width: 1.5 },
};
const HEIGHT = 560;
const hue = (c, n) => Math.round((c * 360) / Math.max(n, 5) + 20) % 360;
const radius = (n, max) => 5 + 17 * Math.sqrt(n / max);

function layout(graph, w, h) {
  const index = new Map(graph.nodes.map((n, i) => [n.id, i]));
  const k = graph.clusters.length;
  const home = graph.clusters.map((c, i) => [w / 2 + Math.cos((i / k) * 6.283) * w * 0.28, h / 2 + Math.sin((i / k) * 6.283) * h * 0.28]);
  const max = Math.max(1, ...graph.nodes.map((n) => n.plays));
  const nodes = graph.nodes.map((n, i) => {
    const [hx, hy] = home[n.cluster] || [w / 2, h / 2];
    const a = i * 2.4;
    return { ...n, x: hx + Math.cos(a) * 40 * Math.sqrt(i % 9), y: hy + Math.sin(a) * 40 * Math.sqrt(i % 9), vx: 0, vy: 0, r: radius(n.plays, max), hx, hy };
  });
  const links = graph.edges.map((e) => ({ ...e, s: nodes[index.get(e.a)], t: nodes[index.get(e.b)] }));
  return { nodes, links };
}

function tick(sim, alpha, w, h, pinned) {
  const { nodes, links } = sim;
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy || 0.01;
      const min = a.r + b.r + 6;
      if (d2 > 90000) continue;
      const d = Math.sqrt(d2), f = (1400 / d2 + (d < min ? (min - d) * 0.3 : 0)) * alpha;
      dx /= d; dy /= d;
      a.vx -= dx * f; a.vy -= dy * f; b.vx += dx * f; b.vy += dy * f;
    }
  }
  for (const l of links) {
    const dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
    const f = (d - (70 + 50 / (1 + l.weight))) * 0.03 * Math.min(2, 0.5 + l.weight) * alpha;
    l.s.vx += (dx / d) * f; l.s.vy += (dy / d) * f; l.t.vx -= (dx / d) * f; l.t.vy -= (dy / d) * f;
  }
  for (const n of nodes) {
    n.vx += (n.hx - n.x) * 0.012 * alpha + (w / 2 - n.x) * 0.002 * alpha;
    n.vy += (n.hy - n.y) * 0.012 * alpha + (h / 2 - n.y) * 0.002 * alpha;
    if (n === pinned) { n.vx = n.vy = 0; continue; }
    n.vx *= 0.82; n.vy *= 0.82;
    n.x = Math.max(n.r, Math.min(w - n.r, n.x + n.vx)); n.y = Math.max(n.r, Math.min(h - n.r, n.y + n.vy));
  }
}

function Network({ graph, kinds, selected, setSelected }) {
  const box = useRef(null), canvas = useRef(null), state = useRef({});
  const theme = useThemeTick();
  const clusters = graph.clusters.length;

  useEffect(() => {
    const el = canvas.current, w = box.current.clientWidth, dpr = window.devicePixelRatio || 1;
    el.width = w * dpr; el.height = HEIGHT * dpr; el.style.height = `${HEIGHT}px`;
    const sim = layout(graph, w, HEIGHT), s = state.current;
    Object.assign(s, { sim, w, alpha: 1, dpr, pinned: null, hover: null });
    let frame;
    const run = () => {
      s.alpha = Math.max(0.02, s.alpha * 0.985);
      tick(sim, s.alpha, w, HEIGHT, s.pinned);
      s.draw?.();
      if (s.alpha > 0.021 || s.pinned) frame = requestAnimationFrame(run);
    };
    s.wake = () => { s.alpha = Math.max(s.alpha, 0.3); cancelAnimationFrame(frame); frame = requestAnimationFrame(run); };
    frame = requestAnimationFrame(run);
    return () => cancelAnimationFrame(frame);
  }, [graph]);

  useEffect(() => {
    const s = state.current, el = canvas.current;
    s.draw = () => {
      if (!s.sim) return;
      const ctx = el.getContext("2d"), css = getComputedStyle(el);
      const fg = css.getPropertyValue("--fg").trim() || "#222", line = css.getPropertyValue("--line").trim() || "#ccc", accent = css.getPropertyValue("--accent").trim() || "#2a7";
      ctx.setTransform(s.dpr, 0, 0, s.dpr, 0, 0);
      ctx.clearRect(0, 0, s.w, HEIGHT);
      const near = new Set();
      if (selected) near.add(selected);
      for (const l of s.sim.links) if (l.a === selected || l.b === selected) { near.add(l.a); near.add(l.b); }
      for (const l of s.sim.links) {
        const shown = Object.keys(l.kinds).filter((k) => kinds[k]);
        if (!shown.length) continue;
        const kind = shown.includes("collab") ? "collab" : shown[0], on = !selected || l.a === selected || l.b === selected;
        ctx.setLineDash(KINDS[kind].dash); ctx.lineWidth = KINDS[kind].width;
        ctx.strokeStyle = kind === "similar" ? accent : on && selected ? fg : line;
        ctx.globalAlpha = on ? (selected ? 0.9 : 0.75) : 0.15;
        ctx.beginPath(); ctx.moveTo(l.s.x, l.s.y); ctx.lineTo(l.t.x, l.t.y); ctx.stroke();
      }
      ctx.setLineDash([]);
      ctx.font = "12px 'DM Sans', sans-serif"; ctx.textAlign = "center";
      for (const n of s.sim.nodes) {
        const on = !selected || near.has(n.id);
        ctx.globalAlpha = on ? 1 : 0.2;
        ctx.fillStyle = `hsl(${hue(n.cluster, clusters)} 60% 52%)`;
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, 6.283); ctx.fill();
        if (n.id === selected || n === s.hover) { ctx.strokeStyle = fg; ctx.lineWidth = 2; ctx.stroke(); }
        if (on && (n.r > 11 || n.id === selected || n === s.hover || near.has(n.id) && selected)) {
          ctx.fillStyle = fg; ctx.fillText(n.id.length > 22 ? `${n.id.slice(0, 21)}…` : n.id, n.x, n.y + n.r + 13);
        }
      }
      ctx.globalAlpha = 1;
    };
    s.draw();
  }, [graph, kinds, selected, theme, clusters]);

  const at = (e) => {
    const box = canvas.current.getBoundingClientRect(), x = e.clientX - box.left, y = e.clientY - box.top;
    return [x, y, [...state.current.sim.nodes].reverse().find((n) => (n.x - x) ** 2 + (n.y - y) ** 2 <= (n.r + 3) ** 2)];
  };
  const down = (e) => {
    const [, , n] = at(e), s = state.current;
    if (!n) { setSelected(null); return; }
    s.pinned = n; s.moved = false; canvas.current.setPointerCapture(e.pointerId); s.wake();
  };
  const move = (e) => {
    const [x, y, n] = at(e), s = state.current;
    if (s.pinned) { s.pinned.x = x; s.pinned.y = y; s.moved = true; s.wake(); return; }
    if (n !== s.hover) { s.hover = n; canvas.current.style.cursor = n ? "pointer" : ""; s.draw?.(); }
  };
  const up = () => {
    const s = state.current;
    if (s.pinned && !s.moved) setSelected(s.pinned.id === selected ? null : s.pinned.id);
    s.pinned = null;
  };
  return html`<div ref=${box}><canvas ref=${canvas} class="netgraph" role="img" aria-label="Network of the artists you listen to"
    onPointerDown=${down} onPointerMove=${move} onPointerUp=${up} onPointerLeave=${() => { state.current.hover = null; }}></canvas></div>`;
}

function Detail({ graph, id }) {
  const node = graph.nodes.find((n) => n.id === id);
  const cluster = graph.clusters[node.cluster];
  const rows = graph.edges.filter((e) => e.a === id || e.b === id).map((e) => ({ other: e.a === id ? e.b : e.a, kinds: e.kinds, weight: e.weight }))
    .sort((a, b) => b.weight - a.weight);
  const why = (k, v) => k === "colisten" ? `played together ${v}×` : k === "collab" ? `${v} shared track${v > 1 ? "s" : ""}` : k === "credit" ? (typeof v === "number" ? `wrote ${v} track${v > 1 ? "s" : ""} for` : `composer ${v}`) : v || "similar";
  return html`<div class="netdetail"><h3>${id}</h3>
    <p class="muted small">${num(node.plays)} plays${cluster ? ` · ${cluster.label}` : ""}</p>
    ${rows.length ? html`<ul>${rows.map((r) => html`<li key=${r.other}><b>${r.other}</b> <span class="muted">${Object.entries(r.kinds).map(([k, v]) => why(k, v)).join(" · ")}</span></li>`)}</ul>`
      : html`<p class="muted small">No connections yet.</p>`}</div>`;
}

export function MusicGraph({ days }) {
  const [g, setG] = useState(null);
  const [kinds, setKinds] = useState({ colisten: true, collab: true, credit: true, similar: true });
  const [selected, setSelected] = useState(null);
  const [asking, setAsking] = useState(false);
  useEffect(() => { setG(null); setSelected(null); api("/api/music/graph", { params: { days } }).then(setG).catch(() => setG({ nodes: [] })); }, [days]);
  const ask = async () => {
    setAsking(true);
    try { setG(await api("/api/music/graph/ask", { method: "POST", params: { days } })); } catch (e) { toast(e.message); }
    setAsking(false);
  };
  const present = useMemo(() => new Set(g?.edges?.flatMap((e) => Object.keys(e.kinds)) || []), [g]);
  if (!g || g.nodes.length < 3) return null;
  const select = (id) => setSelected(id);
  return html`<div class="card"><h2>Artist network</h2>
    <p class="muted small">${num(g.nodes.length)} of ${num(g.total_artists)} artists, sized by plays and colored by cluster. Drag to rearrange, click to see connections.</p>
    <div class="netkinds">${Object.entries(KINDS).filter(([k]) => present.has(k) || k === "similar").map(([k, v]) => html`<label key=${k}><input type="checkbox" checked=${kinds[k]}
        onChange=${() => setKinds({ ...kinds, [k]: !kinds[k] })} /> ${v.label}</label>`)}
      ${g.can_ask && html`<button disabled=${asking} onClick=${ask}>${asking ? "Asking Claude…" : g.ai.asked ? "Refresh AI similarity" : "Add AI similarity"}</button>`}</div>
    <${Network} graph=${g} kinds=${kinds} selected=${selected} setSelected=${setSelected} />
    ${selected && g.nodes.some((n) => n.id === selected) && html`<${Detail} graph=${g} id=${selected} />`}
    <div class="cols"><div><h3>Clusters</h3><ul class="netlist">${g.clusters.filter((c) => c.size > 1).map((c) => html`<li key=${c.id}>
        <i style=${`background:hsl(${hue(c.id, g.clusters.length)} 60% 52%)`}></i><b>${c.label}</b> <span class="muted">${c.size}: ${c.members.slice(0, 5).join(", ")}${c.size > 5 ? "…" : ""}</span></li>`)}</ul></div>
      <div><h3>Hidden bridges</h3>${g.bridges.length ? html`<ul class="netlist">${g.bridges.map((b) => html`<li key=${b.id}><button class="link" onClick=${() => select(b.id)}>${b.id}</button>
        <span class="muted">${num(b.plays)} plays, ties ${b.links.join(" ↔ ")}</span></li>`)}</ul>` : html`<p class="muted small">None yet.</p>`}</div></div>
    ${!g.ai.asked && !g.can_ask && html`<p class="muted small">Add an Anthropic key in Settings → Vision to link artists by sound and scene too.</p>`}</div>`;
}
