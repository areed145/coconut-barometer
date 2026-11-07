// Music → Explore (#/music/explore, music_explore.py, issue #204): the whole artist graph, a piece at a time. Start
// from an artist, a scene, a genre, a decade or a country; show an artist's neighbours to grow the map; ask how two
// artists connect. Each answer joins what's shown, with its links to it, and the network moves to make room.
import { html, useState, useEffect, useMemo, useRef, api, num, toast } from "./lib.js";
import { Network, Detail, KINDS, pairKey, why } from "./music_graph.js";

const STARTS = [["artist", "Artist"], ["scene", "Scene"], ["genre", "Genre"], ["decade", "Decade"], ["country", "Country"]];
const MAX_SHOWN = 400;                          // past this the layout gets slow on a phone
const EMPTY = { nodes: new Map(), edges: new Map() };

function Chips({ items, onPick }) {
  return html`<span class="netchips">${items.map(([value, label]) => html`<button key=${value} class="chip" onClick=${() => onPick(value)}>${label}</button>`)}</span>`;
}

function Start({ facets, onArtist, onFilter }) {
  const top = facets.artists.slice(0, 8).map((a) => [a.id, a.id]);
  const scenes = facets.scenes.slice(0, 10).map(([s, n]) => [s, `${s} (${n})`]);
  const decades = facets.decades.filter(([, n]) => n >= 3).map(([d]) => [d, `${d}s`]);
  return html`<div class="card"><h2>Where to start</h2>
    <p class="muted small">${num(facets.artists.length)} artists with over ${num(facets.min_plays)} plays and ${num(facets.edges)} links between them.
      Pick one below, or search above.</p>
    <p class="small"><b>Most played</b> <${Chips} items=${top} onPick=${onArtist} /></p>
    ${scenes.length > 0 && html`<p class="small"><b>Scenes</b> <${Chips} items=${scenes} onPick=${(v) => onFilter({ scene: v })} /></p>`}
    ${decades.length > 0 && html`<p class="small"><b>Decades</b> <${Chips} items=${decades} onPick=${(v) => onFilter({ decade: v })} /></p>`}
    ${!scenes.length && html`<p class="muted small">Scenes and decades appear once artists are profiled: turn on artist profiles in <a href="#/settings/music">Settings → Music</a>.</p>`}</div>`;
}

export function Explorer({ start }) {
  const [facets, setFacets] = useState(null);
  const [error, setError] = useState(null);
  const [map, setMap] = useState(EMPTY);
  const [clusters, setClusters] = useState([]);
  const [kinds, setKinds] = useState({ collab: true, musicbrainz: true, credit: true, profile: true });
  const [selected, setSelected] = useState(null);
  const [focusOn, setFocusOn] = useState(null);
  const [fitOn, setFitOn] = useState(null);
  const [highlight, setHighlight] = useState(null);
  const [path, setPath] = useState(null);
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState("artist");
  const [value, setValue] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const shown = useRef(EMPTY);
  shown.current = map;

  useEffect(() => { api("/api/music/explore/facets").then(setFacets).catch((e) => setError(e.message)); }, []);

  // One question to the server; its answer joins the map (or replaces it), links and all.
  const ask = async (what, body, replace = false) => {
    setBusy(true);
    try {
      const have = replace ? [] : [...shown.current.nodes.keys()].slice(0, 600);
      const r = await api(`/api/music/explore/${what}`, { method: "POST", body: { ...body, have } });
      const nodes = new Map(replace ? [] : shown.current.nodes), edges = new Map(replace ? [] : shown.current.edges);
      const fresh = r.nodes.filter((n) => !nodes.has(n.id));
      if (nodes.size + fresh.length > MAX_SHOWN) { toast(`That would show over ${MAX_SHOWN} artists: remove some or start over`); return null; }
      for (const n of r.nodes) nodes.set(n.id, n);
      for (const e of r.edges) if (nodes.has(e.a) && nodes.has(e.b)) edges.set(pairKey(e.a, e.b), e);
      setMap({ nodes, edges });
      setClusters(r.clusters);
      return r;
    } catch (e) { toast(e.message); return null; } finally { setBusy(false); }
  };
  const go = (id) => { setSelected(id); setFocusOn({ id, at: Date.now() }); };
  const expand = async (id, depth = 1) => {
    const r = await ask("neighbourhood", { artist: id, depth });
    if (r) { setHighlight(null); setPath(null); setSelected(r.focus); setFitOn(Date.now()); }
  };
  const filter = async (want) => {
    const r = await ask("filter", want);
    if (!r) return;
    if (!r.nodes.length) { toast("No artists match"); return; }
    const ids = new Set(r.nodes.map((n) => n.id));
    setSelected(null); setPath(null);
    setHighlight({ nodes: ids, edges: new Set(r.edges.filter((e) => ids.has(e.a) && ids.has(e.b)).map((e) => pairKey(e.a, e.b))) });
    if (r.matched > r.nodes.length) toast(`${num(r.matched)} artists match: showing the ${r.nodes.length} most played`);
  };
  const connect = async (e) => {
    e?.preventDefault();
    if (!from.trim() || !to.trim()) return;
    const r = await ask("path", { a: from.trim(), b: to.trim() });
    if (!r) return;
    setPath(r); setSelected(null);
    setHighlight({ nodes: new Set(r.path), edges: new Set(r.steps.map((s) => pairKey(s.a, s.b))) });
  };
  const begin = (e) => {
    e.preventDefault();
    const v = value.trim();
    if (!v) return;
    if (kind === "artist") expand(v);
    else filter({ [kind]: kind === "decade" ? parseInt(v, 10) : v });
  };
  const remove = (id) => {
    const nodes = new Map(map.nodes), edges = new Map(map.edges);
    nodes.delete(id);
    for (const [k, e] of edges) if (e.a === id || e.b === id) edges.delete(k);
    setMap({ nodes, edges }); setSelected(null);
  };
  const clear = () => { setMap(EMPTY); setSelected(null); setHighlight(null); setPath(null); };
  useEffect(() => { if (start && facets) expand(start); }, [start, !!facets]);

  // What the network draws: the clusters present, numbered from 0 so their colors spread.
  const graph = useMemo(() => {
    const ids = [...new Set([...map.nodes.values()].map((n) => n.cluster))].sort((a, b) => a - b);
    const local = new Map(ids.map((c, i) => [c, i]));
    return { nodes: [...map.nodes.values()].map((n) => ({ ...n, cluster: local.get(n.cluster) })), edges: [...map.edges.values()],
             clusters: ids.map((c) => clusters[c] || { id: c, label: "" }) };
  }, [map, clusters]);
  const counts = useMemo(() => {
    const c = {};
    for (const e of graph.edges) for (const k in e.kinds) c[k] = (c[k] || 0) + 1;
    return c;
  }, [graph]);
  const options = facets && {
    artist: facets.artists.map((a) => a.id), scene: facets.scenes.map(([s]) => s), genre: facets.genres.map(([g]) => g),
    decade: facets.decades.map(([d]) => String(d)), country: facets.countries.map(([c]) => c),
  };

  if (error) return html`<p class="problem">${error}</p>`;
  if (!facets) return html`<div class="boot">Building the whole graph…</div>`;
  if (facets.artists.length < 2) return html`<div class="card"><h2>Explore</h2><p class="muted">Not enough listening yet: the explorer shows artists with over ${num(facets.min_plays)} plays.</p></div>`;
  const toolbar = html`<div class="toolbar">
    <form class="netstart" onSubmit=${begin}>
      <select aria-label="Start from" value=${kind} onChange=${(e) => { setKind(e.target.value); setValue(""); }}>
        ${STARTS.map(([k, label]) => html`<option key=${k} value=${k}>${label}</option>`)}</select>
      <input type="search" list="explore-options" aria-label="Value" placeholder=${kind === "decade" ? "1970" : kind === "country" ? "US" : `Find ${kind === "artist" ? "an artist" : "a " + kind}`}
        value=${value} onInput=${(e) => setValue(e.target.value)} />
      <datalist id="explore-options">${options[kind].map((o) => html`<option key=${o} value=${o} />`)}</datalist>
      <button class="primary" disabled=${busy}>Add</button></form>
    ${map.nodes.size > 0 && html`<button onClick=${clear}>Start over</button>`}</div>`;
  const detailExtra = selected && map.nodes.has(selected) && html`<p class="small netactions">
    <button class="link" disabled=${busy} onClick=${() => expand(selected)}>Show neighbours</button>
    <button class="link" disabled=${busy} onClick=${() => expand(selected, 2)}>Two steps out</button>
    <button class="link" onClick=${() => { setFrom(selected); toast("Now pick who to connect to, below"); }}>Connect from here</button>
    <button class="link" onClick=${() => remove(selected)}>Remove</button></p>`;
  return html`${toolbar}
    ${map.nodes.size === 0 ? html`<${Start} facets=${facets} onArtist=${expand} onFilter=${filter} />` : html`
    <div class="card"><h2>Explore</h2>
      <p class="muted small">${num(map.nodes.size)} of ${num(facets.artists.length)} artists shown. Click an artist for its connections and to show its neighbours.
        ${highlight && html` <button class="link" onClick=${() => { setHighlight(null); setPath(null); }}>Clear highlight</button>`}</p>
      <div class="netkinds">${Object.entries(KINDS).map(([k, v]) => html`<label key=${k}><input type="checkbox" checked=${kinds[k]} onChange=${() => setKinds({ ...kinds, [k]: !kinds[k] })} />
          ${v.label} <span class="muted">${num(counts[k] || 0)}</span></label>`)}</div>
      <${Network} graph=${graph} kinds=${kinds} selected=${selected} setSelected=${setSelected} cluster=${null} focusOn=${focusOn} highlight=${highlight} fitOn=${fitOn} />
      ${selected && map.nodes.has(selected) && html`<${Detail} graph=${graph} id=${selected} kinds=${kinds} go=${(id) => (map.nodes.has(id) ? go(id) : expand(id))} extra=${detailExtra} />`}</div>`}
    <div class="card"><h2>How are they connected?</h2>
      <form class="netstart" onSubmit=${connect}>
        <input type="search" list="explore-artists" aria-label="From artist" placeholder="From" value=${from} onInput=${(e) => setFrom(e.target.value)} />
        <input type="search" list="explore-artists" aria-label="To artist" placeholder="To" value=${to} onInput=${(e) => setTo(e.target.value)} />
        <datalist id="explore-artists">${options.artist.map((o) => html`<option key=${o} value=${o} />`)}</datalist>
        <button class="primary" disabled=${busy}>Connect</button></form>
      ${path && html`<ul class="netpath">${path.path.map((id, i) => html`<li key=${id}><button class="link" onClick=${() => go(id)}>${id}</button></li>
          ${i < path.steps.length && html`<li key=${id + "→"} class="muted step">↓ ${Object.entries(path.steps[i].kinds).map(([k, v]) => why(k, v)).join(" · ")}</li>`}`)}</ul>`}</div>`;
}
