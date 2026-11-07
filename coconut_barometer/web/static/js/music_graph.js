// Music → Artist network (#/music/graph, music_graph.py): a force layout drawn on a canvas, run here in the browser.
// Size is plays, color is cluster; the line style says why two artists are linked. The layout keeps each artist's
// place when the range, the number drawn or the kinds shown change, or MusicBrainz fills in more links, so the network
// moves to its new shape instead of starting over. Wheel or pinch to zoom, drag the background to pan, drag an artist
// to pull it, click to see its connections, double-click to zoom to its neighbours.
import { html, useState, useEffect, useRef, useMemo, api, num, toast, every, useThemeTick } from "./lib.js";
import { Range } from "./viz.js";

const KINDS = {
  collab: { label: "Collaborations", dash: [], width: 3 },
  musicbrainz: { label: "MusicBrainz credits", dash: [], width: 2, color: "--chart-3" },
  credit: { label: "Composers", dash: [7, 4], width: 1.5 },
  similar: { label: "AI: similar", dash: [2, 4], width: 1.5, color: "--accent" },
};
const DRAW_ORDER = ["collab", "musicbrainz", "credit", "similar"];   // a link is drawn as its first kind shown
const SIZES = [50, 100, 150, 200];
const ALL = 3650;
const hue = (c, n) => Math.round((c * 360) / Math.max(n, 5) + 20) % 360;
const color = (c, n) => `hsl(${hue(c, n)} 60% 52%)`;
const radius = (n, max) => 5 + 17 * Math.sqrt(n / max);
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
  const links = graph.edges.map((e) => ({ ...e, s: index.get(e.a), t: index.get(e.b) })).filter((l) => l.s && l.t);
  nodes.forEach((n, i) => {                     // a new artist starts beside one it's linked to, else near its cluster
    if (!n.fresh) return;
    const by = links.find((l) => (l.s === n && !l.t.fresh) || (l.t === n && !l.s.fresh));
    const near = by ? (by.s === n ? by.t : by.s) : null, a = i * 2.4, spread = near ? 30 : 40 * Math.sqrt(i % 9);
    n.x = (near ? near.x : n.hx) + Math.cos(a) * spread;
    n.y = (near ? near.y : n.hy) + Math.sin(a) * spread;
  });
  return { nodes, links, index };
}

function tick(sim, alpha, w, h, pinned, kinds) {
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
    let shown = 0;
    for (const k in l.kinds) if (kinds[k]) shown += 1;
    if (!shown) continue;
    const dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
    const f = (d - (70 + 50 / (1 + l.weight))) * 0.03 * Math.min(2, 0.4 + l.weight * shown * 0.5) * alpha;
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

function bounds(nodes) {
  if (!nodes.length) return null;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of nodes) { x0 = Math.min(x0, n.x - n.r); y0 = Math.min(y0, n.y - n.r); x1 = Math.max(x1, n.x + n.r); y1 = Math.max(y1, n.y + n.r + 14); }
  return { x0, y0, x1, y1 };
}

function Network({ graph, kinds, selected, setSelected, cluster, focusOn }) {
  const box = useRef(null), canvas = useRef(null), tip = useRef(null), s = useRef({}).current;
  const theme = useThemeTick();
  const [h, setH] = useState(height);

  // The loop: steps the forces while they settle, eases the view to where it's going, marches the selected artist's
  // links, and stops when nothing moves.
  useEffect(() => {
    Object.assign(s, { alpha: 1, view: { k: 1, x: 0, y: 0 }, goal: null, pointers: new Map(), hover: null, dash: 0 });
    const run = (t) => {
      s.frame = null;
      if (s.sim && s.alpha > 0.02) { s.alpha *= 0.985; tick(s.sim, s.alpha, s.w, s.h, s.pinned, s.kinds); }
      if (s.goal) {
        const v = s.view, g = s.goal;
        v.k += (g.k - v.k) * 0.18; v.x += (g.x - v.x) * 0.18; v.y += (g.y - v.y) * 0.18;
        if (Math.abs(g.k - v.k) < 0.002 && Math.abs(g.x - v.x) < 0.5 && Math.abs(g.y - v.y) < 0.5) { Object.assign(v, g); s.goal = null; }
      }
      if (s.autofit && s.alpha < 0.5) { s.autofit = false; s.fit(); }
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

  useEffect(() => {
    const first = !s.sim;
    s.sim = rebuild(graph, s.sim, s.w, s.h);
    s.autofit = first;
    s.wake(first ? 1 : 0.5);
  }, [graph]);
  useEffect(() => { s.kinds = kinds; s.wake(0.35); }, [kinds]);
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
      if (focus) for (const l of s.sim.links) if ((l.a === focus || l.b === focus) && Object.keys(l.kinds).some((x) => k[x])) { near.add(l.a); near.add(l.b); }
      const inCluster = (n) => s.cluster == null || n.cluster === s.cluster;
      for (const l of s.sim.links) {
        const kind = DRAW_ORDER.find((x) => k[x] && x in l.kinds);
        if (!kind) continue;
        const mine = focus && (l.a === focus || l.b === focus), on = focus ? mine : inCluster(l.s) && inCluster(l.t);
        const style = KINDS[kind], marching = mine && s.selected;
        ctx.setLineDash((marching && !style.dash.length ? [6, 4] : style.dash).map((d) => d / v.k));
        ctx.lineDashOffset = marching ? -s.dash / v.k : 0;
        ctx.lineWidth = (style.width * (0.6 + Math.min(1.5, l.weight) * 0.5)) / Math.sqrt(v.k);
        ctx.strokeStyle = style.color ? value(style.color, fg) : mine ? fg : line;
        ctx.globalAlpha = on ? (focus ? 0.95 : 0.7) : 0.08;
        ctx.beginPath(); ctx.moveTo(l.s.x, l.s.y); ctx.lineTo(l.t.x, l.t.y); ctx.stroke();
      }
      ctx.setLineDash([]); ctx.lineDashOffset = 0;
      ctx.font = `${12 / v.k}px 'DM Sans', sans-serif`; ctx.textAlign = "center";
      for (const n of s.sim.nodes) {
        const on = focus ? near.has(n.id) : inCluster(n);
        ctx.globalAlpha = on ? 1 : 0.15;
        ctx.fillStyle = color(n.cluster, clusters);
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, 6.283); ctx.fill();
        if (n.id === s.selected || n === s.hover) { ctx.strokeStyle = fg; ctx.lineWidth = 2 / v.k; ctx.stroke(); }
        if (on && (n.r * v.k > 11 || n.id === s.selected || n === s.hover || (focus && near.has(n.id)) || (s.cluster != null && v.k > 1.2))) {
          ctx.fillStyle = fg; ctx.fillText(n.id.length > 24 ? `${n.id.slice(0, 23)}…` : n.id, n.x, n.y + n.r + 13 / v.k);
        }
      }
      ctx.globalAlpha = 1;
    };
    s.draw();
  }, [graph, kinds, theme]);

  // ---- pointers: drag an artist, pan, pinch, hover ----
  const world = (e) => {
    const b = canvas.current.getBoundingClientRect(), v = s.view, sx = e.clientX - b.left, sy = e.clientY - b.top;
    return { sx, sy, x: (sx - v.x) / v.k, y: (sy - v.y) / v.k };
  };
  const hit = (p) => s.sim && [...s.sim.nodes].reverse().find((n) => (n.x - p.x) ** 2 + (n.y - p.y) ** 2 <= (n.r + 4 / s.view.k) ** 2);
  const showTip = (n, p) => {
    const el = tip.current;
    if (!n) { el.hidden = true; return; }
    const links = s.sim.links.filter((l) => (l.a === n.id || l.b === n.id) && Object.keys(l.kinds).some((x) => s.kinds?.[x])).length;
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

const why = (k, v) => k === "collab" ? `${v} shared track${v > 1 ? "s" : ""}`
  : k === "credit" ? (typeof v === "number" ? `wrote ${v} track${v > 1 ? "s" : ""} for` : `composer ${v}`)
  : k === "musicbrainz" ? v || "MusicBrainz" : v || "similar";

function Detail({ graph, id, kinds, go }) {
  const node = graph.nodes.find((n) => n.id === id);
  const cluster = graph.clusters[node.cluster];
  const rows = graph.edges.filter((e) => (e.a === id || e.b === id) && Object.keys(e.kinds).some((k) => kinds[k]))
    .map((e) => ({ other: e.a === id ? e.b : e.a, kinds: Object.entries(e.kinds).filter(([k]) => kinds[k]), weight: e.weight }))
    .sort((a, b) => b.weight - a.weight);
  return html`<div class="netdetail"><h3>${id}</h3>
    <p class="muted small">${num(node.plays)} plays · ${num(node.minutes)} minutes${node.genre ? ` · ${node.genre}` : ""}${cluster ? ` · ${cluster.label}` : ""}</p>
    ${rows.length ? html`<ul>${rows.map((r) => html`<li key=${r.other}><button class="link" onClick=${() => go(r.other)}>${r.other}</button>
        <span class="muted"> ${r.kinds.map(([k, v]) => why(k, v)).join(" · ")}</span></li>`)}</ul>`
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

export function ArtistNetwork({ focus }) {
  const [days, setDays] = useState(ALL);
  const [limit, setLimit] = useState(100);
  const [g, setG] = useState(null);
  const [error, setError] = useState(null);
  const [kinds, setKinds] = useState({ collab: true, musicbrainz: true, credit: true, similar: true });
  const [selected, setSelected] = useState(null);
  const [cluster, setCluster] = useState(null);
  const [focusOn, setFocusOn] = useState(null);
  const [query, setQuery] = useState("");
  const [asking, setAsking] = useState(false);
  const [looking, setLooking] = useState(false);
  const go = (id) => { setSelected(id); setCluster(null); setFocusOn({ id, at: Date.now() }); };
  const load = () => api("/api/music/graph", { params: { days, limit } }).then((x) => { setG(x); setError(null); }).catch((e) => setError(e.message));
  useEffect(() => { load(); }, [days, limit]);
  const filling = !!(g?.musicbrainz?.ready && g.musicbrainz.asked < g.musicbrainz.tracks);
  useEffect(() => (filling ? every(load, 120000) : undefined), [filling, days, limit]);   // MusicBrainz's links arrive as the jobs finish
  useEffect(() => { if (focus && g?.nodes.some((n) => n.id === focus)) go(focus); }, [focus, !!g]);
  const ask = async () => {
    setAsking(true);
    try { setG(await api("/api/music/graph/ask", { method: "POST", params: { days, limit } })); } catch (e) { toast(e.message); }
    setAsking(false);
  };
  const lookup = async () => {
    setLooking(true);
    try {
      const r = await api("/api/music/graph/musicbrainz", { method: "POST" });
      toast(r.status === "queued" ? "Queued: it runs after the jobs ahead of it (Status → Jobs)"
        : r.status === "busy" ? "A lookup is already waiting or running" : "MusicBrainz isn't set up");
    } catch (e) { toast(e.message); }
    setLooking(false);
  };
  const counts = useMemo(() => {
    const c = {};
    for (const e of g?.edges || []) for (const k in e.kinds) c[k] = (c[k] || 0) + 1;
    return c;
  }, [g]);
  const search = (e) => {
    e.preventDefault();
    const q = query.trim().toLowerCase();
    const hit = q && (g.nodes.find((n) => n.id.toLowerCase() === q) || g.nodes.find((n) => n.id.toLowerCase().includes(q)));
    if (hit) go(hit.id); else toast("Not among the artists drawn");
  };
  const toolbar = html`<div class="toolbar"><${Range} days=${days} setDays=${setDays} />
    <span class="seg" aria-label="Artists drawn">${SIZES.map((n) => html`<button key=${n} class=${limit === n ? "primary" : ""} onClick=${() => setLimit(n)}>${n}</button>`)}</span>
    ${g?.nodes?.length > 0 && html`<form class="netsearch" onSubmit=${search}><input type="search" list="net-artists" placeholder="Find an artist" aria-label="Find an artist"
        value=${query} onInput=${(e) => setQuery(e.target.value)} /><datalist id="net-artists">${g.nodes.map((n) => html`<option key=${n.id} value=${n.id} />`)}</datalist></form>`}</div>`;
  if (error) return html`${toolbar}<p class="problem">${error}</p>`;
  if (!g) return html`${toolbar}<div class="boot">Loading…</div>`;
  if (g.nodes.length < 3) return html`${toolbar}<div class="card"><h2>Artist network</h2><p class="muted">Not enough listening in this range yet.</p>
    <${MusicBrainzNote} mb=${g.musicbrainz} onAsk=${lookup} asking=${looking} /></div>`;
  const shown = Object.entries(KINDS).filter(([k]) => counts[k] || k === "similar" || k === "musicbrainz");
  return html`${toolbar}
    <div class="card"><h2>Artist network</h2>
      <p class="muted small">${num(g.nodes.length)} of ${num(g.total_artists)} artists, sized by plays and colored by cluster. Scroll or pinch to zoom, drag to pan or pull an artist,
        click for its connections, double-click to zoom to its neighbours.</p>
      <div class="netkinds">${shown.map(([k, v]) => html`<label key=${k}><input type="checkbox" checked=${kinds[k]} onChange=${() => setKinds({ ...kinds, [k]: !kinds[k] })} />
          ${v.label} <span class="muted">${num(counts[k] || 0)}</span></label>`)}
        ${g.can_ask && html`<button disabled=${asking} onClick=${ask}>${asking ? "Asking Claude…" : g.ai.asked ? "Refresh AI similarity" : "Add AI similarity"}</button>`}</div>
      <${Network} graph=${g} kinds=${kinds} selected=${selected} setSelected=${setSelected} cluster=${cluster} focusOn=${focusOn} />
      ${selected && g.nodes.some((n) => n.id === selected) && html`<${Detail} graph=${g} id=${selected} kinds=${kinds} go=${go} />`}
      <${MusicBrainzNote} mb=${g.musicbrainz} onAsk=${lookup} asking=${looking} />
      ${!g.ai.asked && !g.can_ask && html`<p class="muted small">Add an Anthropic key in Settings → Vision to link artists by sound and scene too.</p>`}</div>
    <div class="cols"><div class="card"><h2>Clusters</h2><ul class="netlist">${g.clusters.filter((c) => c.size > 1).map((c) => html`<li key=${c.id}>
        <button class="link" aria-pressed=${cluster === c.id} onClick=${() => { setSelected(null); setCluster(cluster === c.id ? null : c.id); }}>
          <i style=${`background:${color(c.id, g.clusters.length)}`}></i>${c.label}</button>
        <span class="muted"> ${c.size}: ${c.members.slice(0, 5).join(", ")}${c.size > 5 ? "…" : ""}</span></li>`)}</ul>
        ${cluster != null && html`<button class="link" onClick=${() => setCluster(null)}>Show every cluster</button>`}</div>
      <div class="card"><h2>Hidden bridges</h2>${g.bridges.length ? html`<ul class="netlist">${g.bridges.map((b) => html`<li key=${b.id}><button class="link" onClick=${() => go(b.id)}>${b.id}</button>
        <span class="muted"> ${num(b.plays)} plays, ties ${b.links.join(" ↔ ")}</span></li>`)}</ul>` : html`<p class="muted small">None yet.</p>`}</div></div>`;
}
