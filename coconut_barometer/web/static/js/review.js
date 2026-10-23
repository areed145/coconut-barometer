// The reviewer: every photo sent to review, one at a time. For each: Apply (the changes you keep,
// as edited), Ignore (out of review, nothing changed) or Re-process (a fresh look; it stays).
// Applying and processing run in the server's background worker; this page polls for outcomes.
import { html, useState, useEffect, useRef, api, num, day, plain, toast, photosLink, every } from "./lib.js";
import { leaflet, baseMap } from "./leaflet.js";
import { ReviewViews } from "./review_bulk.js";

// "new": waiting to be processed (processed when you reach it); "not_queued": opened by ID, not in review.
const STATE = { new: "waiting", pending: "ready", applying: "applying", processing: "processing", not_queued: "not in review" };

// Flickr's normalized tag form (as in util.norm_tag), to tell which existing tags a replacement drops.
const norm = (t) => t.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9:=]+/g, "");

function initialChoices(item) {
  return item.changes.map((c) => {
    if (c.action === "set_location") {          // a low-confidence guess waits for you to confirm it
      const has = c.new && c.new.lat != null;
      return { approve: has && c.confidence !== "low", value: has ? c.new : null, tags: null };
    }
    if (c.action === "set_tags")                 // proposed tags on; the photo's current ones off (replaced) unless you keep them
      return { approve: true, value: c.new, tags: c.new.map((t) => ({ t, on: true })), keepOld: false,
               old: (c.old || "").split(/\s+/).filter(Boolean).map((t) => ({ t, on: true })) };
    return { approve: true, value: c.new, tags: null };
  });
}

// set_tags: the photo's tags once applied, reconciled into one list (one setTags call): the proposed tags
// kept, then its current ones kept if that section is on, each once.
function finalTags(ch) {
  const out = [], seen = new Set();
  const add = (t) => { const k = norm(t); if (k && !seen.has(k)) { seen.add(k); out.push(t); } };
  if (ch.approve) ch.tags.filter((x) => x.on).forEach((x) => add(x.t));
  if (ch.keepOld) ch.old.filter((x) => x.on).forEach((x) => add(x.t));
  return out;
}
const sameTags = (a, b) => a.length === b.length && a.every((t) => b.map(norm).includes(norm(t)));

const coords = (v) => `${v.lat.toFixed(4)}, ${v.lon.toFixed(4)}`;
const ACCURACY = (a) => (a >= 15 ? "street" : a >= 12 ? "neighborhood" : a >= 9 ? "town" : a >= 5 ? "region" : "country");

// A map of the guesses: the pin is where it will be set. Click the map to move it (as precise as you're
// zoomed in: Flickr's accuracy is about the zoom level), click a guess to take it, or search for a place.
function LocationMap({ value, options, editable, onPick, focus }) {
  const el = useRef(null), map = useRef(null), layers = useRef(null), pick = useRef(null);
  pick.current = editable ? onPick : () => {};
  useEffect(() => {
    let live = true;
    leaflet().then((lf) => {
      if (!live || !el.current) return;
      const { L } = lf;
      const m = map.current = baseMap(lf, el.current, { scrollWheelZoom: false });
      layers.current = L.layerGroup().addTo(m);
      const pts = [...options.map((o) => [o.lat, o.lon]), ...(value ? [[value.lat, value.lon]] : [])];
      if (pts.length > 1) m.fitBounds(pts, { padding: [30, 30], maxZoom: 12 });
      else if (pts.length) m.setView(pts[0], value ? Math.min(13, Math.max(4, value.accuracy || 11)) : 11);
      else m.setView([20, 0], 2);
      m.on("click", (e) => pick.current({ lat: e.latlng.lat, lon: e.latlng.lng,
                                                       accuracy: Math.min(16, Math.max(1, m.getZoom())), place: "" }));
      draw();
    }).catch(() => {});
    return () => { live = false; map.current?.remove(); map.current = null; };
  }, []);
  const draw = () => {
    const L = window.L, g = layers.current;
    if (!L || !g) return;
    g.clearLayers();
    options.forEach((o) => L.circleMarker([o.lat, o.lon], { radius: 6, color: "#fff", weight: 1.5, fillColor: "#8a8f98", fillOpacity: 0.9 })
      .bindTooltip(Object.assign(document.createElement("span"), { textContent: `${o.place || coords(o)} · ${o.confidence}` })).on("click", (e) => { L.DomEvent.stop(e); pick.current(o); }).addTo(g));
    if (value) L.circleMarker([value.lat, value.lon], { radius: 8, color: "#fff", weight: 2.5, fillColor: "#1dab45", fillOpacity: 1 }).addTo(g);
  };
  useEffect(draw, [value, options]);
  useEffect(() => {                               // a place found by search: show it
    if (!focus || !map.current) return;
    if (focus.bbox) map.current.fitBounds([[focus.bbox[0], focus.bbox[2]], [focus.bbox[1], focus.bbox[3]]], { maxZoom: 16 });
    else map.current.setView([focus.lat, focus.lon], Math.min(16, Math.max(4, focus.accuracy || 11)));
  }, [focus]);
  return html`<div class="locmap" ref=${el}></div>`;
}

function Location({ c, ch, editable, status, set }) {
  const [q, setQ] = useState(""), [found, setFound] = useState(null), [focus, setFocus] = useState(null);
  const options = c.options || [];
  const take = (v) => set({ value: { lat: v.lat, lon: v.lon, accuracy: v.accuracy || 11, place: v.place || v.label || "" }, approve: true });
  const search = async (e) => {
    e.preventDefault(); if (q.trim().length < 2) return;
    try {
      const r = await api("/api/review/places", { params: { q: q.trim() } });
      setFound(r);
      if (r.places.length) { setFocus(r.places[0]); take(r.places[0]); }
    } catch (err) { toast(err.message); }
  };
  const v = ch.value;
  return html`<div class="row"><input type="checkbox" checked=${ch.approve} disabled=${!editable || !v} aria-label="Approve"
      onChange=${(e) => set({ approve: e.target.checked })} />
    <div class="what">Location${status} ${c.confidence && html`<span class=${"status conf-" + c.confidence}>${c.confidence}</span>`}
      <div class="why">${v ? html`<b>${v.place || coords(v)}</b> · ${ACCURACY(v.accuracy)} (${v.accuracy}/16)` : "No location yet: search, or click the map where it was taken."}</div>
      ${c.reason && html`<div class="why">${c.reason}</div>`}
      ${options.length > 1 && html`<div class="chips mt-1">${options.map((o) => html`<button type="button" disabled=${!editable}
        class=${"chip prop" + (v && v.lat === o.lat && v.lon === o.lon ? "" : " off nostrike")} title=${o.reason}
        onClick=${() => take(o)}>${o.place || coords(o)} · ${o.confidence}</button>`)}</div>`}
      ${editable && html`<form class="locsearch mt-1" onSubmit=${search}>
        <input value=${q} onInput=${(e) => setQ(e.target.value)} placeholder="Search a place: Red Rocks, Morrison CO" aria-label="Search a place" />
        <button type="submit">Find</button></form>`}
      ${found && found.places.length > 1 && html`<div class="chips mt-1">${found.places.map((p) => html`<button type="button" class="chip"
        onClick=${() => { setFocus(p); take(p); }}>${p.label.split(",").slice(0, 3).join(",")}</button>`)}</div>`}
      ${found && !found.places.length && html`<div class="why">Nothing found for that.</div>`}
      ${found?.source === "offline" && html`<div class="why">Searched the offline town list (OpenStreetMap couldn't be reached).</div>`}
      <${LocationMap} value=${v} options=${options} editable=${editable} focus=${focus}
        onPick=${(p) => set({ value: { lat: p.lat, lon: p.lon, accuracy: p.accuracy, place: p.place || "" }, approve: true })} />
    </div></div>`;
}

function Change({ c, ch, i, editable, out, onChange }) {
  const why = [c.source, c.confidence, c.reason].filter(Boolean).join(" · ");
  const status = out && out.status ? html` <span class=${"status s-" + out.status}>${out.status}</span>` : "";
  const set = (patch) => onChange(i, { ...ch, ...patch });
  if (c.action === "set_tags") return html`<${TagSections} c=${c} ch=${ch} editable=${editable} out=${out} set=${set} />`;
  if (c.action === "set_location") return html`<${Location} key=${c.photo_id} c=${c} ch=${ch} editable=${editable} status=${status} set=${set} />`;
  const box = html`<input type="checkbox" checked=${ch.approve} disabled=${!editable} aria-label="Approve"
                          onChange=${(e) => set({ approve: e.target.checked })} />`;
  if (c.action === "add_to_album")
    return html`<div class="row">${box}<div class="what">Add to album <b>${c.new}</b>${c.album_id ? "" : " (new album)"}${status}
      <div class="why">${why}</div></div></div>`;
  if (c.action === "add_to_group")
    return html`<div class="row">${box}<div class="what">Add to group <b>${c.new}</b>${status}
      ${c.confidence && html` <span class=${"status conf-" + c.confidence}>${c.confidence}</span>`}<div class="why">${c.reason}</div></div></div>`;
  if (c.action === "set_title")
    return html`<div class="row">${box}<div class="what">Title${status} <span class="why">was: ${c.old || "(blank)"}</span>
      <input type="text" value=${ch.value} disabled=${!editable} onInput=${(e) => set({ value: e.target.value })} /></div></div>`;
  if (c.action === "set_description")
    return html`<div class="row">${box}<div class="what">Description${status}
      ${c.old && html`<div class="replaces">replaces: ${plain(c.old)}</div>`}
      <textarea value=${ch.value} disabled=${!editable} onInput=${(e) => set({ value: e.target.value })}></textarea></div></div>`;
  return html`<div class="row"><div class="what">${c.action}: ${String(c.new)}</div></div>`;
}

// The photo's tags, in two parts like the other changes: the tags processing proposed (on: they go on), and
// the ones it has now (off: they're replaced; on: kept too). Click a tag to drop it from its part.
function TagSections({ c, ch, editable, out, set }) {
  const srcs = c.source.split(",").map((s) => s.replace(/^(std|vision|geo|always):/, "")).join(", ");
  const had = new Set(ch.old.map((x) => norm(x.t)));
  const proposed = new Set(ch.tags.filter((x) => x.on).map((x) => norm(x.t)));
  const final = finalTags(ch), finalSet = new Set(final.map(norm));
  const added = final.filter((t) => !had.has(norm(t))).length, removed = [...had].filter((t) => !finalSet.has(t)).length;
  const chips = (list, key, on, cls) => html`<div class="chips mt-1">${list.map((ts, j) => {
    const st = out?.tags?.[ts.t];
    return html`<button type="button" class=${"chip " + (st || "prop" + (ts.on && on ? "" : " off")) + cls(ts)}
      disabled=${!editable || !on} aria-pressed=${ts.on}
      onClick=${() => set({ [key]: list.map((x, k) => (k === j ? { ...x, on: !x.on } : x)) })}>${ts.t}</button>`;
  })}</div>`;
  return html`
    <div class="row"><input type="checkbox" checked=${ch.approve} disabled=${!editable} aria-label="Use the proposed tags"
        onChange=${(e) => set({ approve: e.target.checked })} />
      <div class="what"><b>Proposed tags</b> <span class="why">· ${srcs} · bold: not on the photo yet${editable ? " · click a tag to drop it" : ""}</span>
        ${chips(ch.tags, "tags", ch.approve, (ts) => (had.has(norm(ts.t)) ? "" : " fresh"))}</div></div>
    <div class="row"><input type="checkbox" checked=${ch.keepOld} disabled=${!editable || !ch.old.length} aria-label="Keep its current tags"
        onChange=${(e) => set({ keepOld: e.target.checked })} />
      <div class="what"><b>Current tags</b> <span class="why">· ${ch.keepOld ? "kept too" : "replaced"}${ch.keepOld && editable ? " · click a tag to drop it" : ""}</span>
        ${ch.old.length ? chips(ch.old, "old", ch.keepOld, (ts) => (ch.approve && proposed.has(norm(ts.t)) ? " also" : ""))
          : html`<div class="why">none</div>`}
        <div class="why mt-1">${sameTags(final, ch.old.map((x) => x.t)) ? "No change to its tags."
          : !final.length ? "No tags kept: its tags stay as they are."
          : `Result: ${final.length} tags · ${added} added · ${removed} removed, in one update.`}</div></div></div>`;
}

function Engagement({ s }) {
  if (!s) return null;
  const growth = s.first_snapshot && s.views != null && s.views > s.first_snapshot.views;
  return html`<div class="card"><h2>Engagement</h2>
    <div class="kpis mb-2">
      ${[["views", s.views], ["faves", s.faves], ["comments", s.comments], ["groups", s.groups.length]].map(([k, v]) =>
        html`<div class="kpi"><span>${k}</span><b>${num(v)}</b></div>`)}
    </div>
    <p class="muted m-0 mb-2">${[growth && `+${num(s.views - s.first_snapshot.views)} views since ${s.first_snapshot.day}`,
      s.license, s.width && `${num(s.width)} × ${num(s.height)}`, s.uploaded && "uploaded " + day(s.uploaded)].filter(Boolean).join(" · ")}</p>
    <dl class="kv">
      ${s.groups.length > 0 && html`<dt>In ${s.groups.length} group${s.groups.length > 1 ? "s" : ""}</dt><dd class="chips">
        ${s.groups.map((g) => html`<a class="chip" href=${`https://www.flickr.com/groups/${encodeURIComponent(g.path_alias || g.id)}/`}
          target="_blank" rel="noopener noreferrer" title=${num(g.members) + " members"}>${g.name}</a>`)}</dd>`}
      ${s.recent_faves.length > 0 && html`<dt>Recently faved by</dt><dd class="chips">
        ${s.recent_faves.map((f) => html`<span class="chip" title=${day(f.date_faved)}>${f.real_name || f.username}</span>`)}</dd>`}
      ${s.recent_comments.length > 0 && html`<dt>Latest comments</dt><dd>${s.recent_comments.map((c) => html`<div class="comment">
        <div class="who">${c.real_name || c.author_name} · ${day(c.date_created)}</div>${plain(c.text)}</div>`)}</dd>`}
    </dl></div>`;
}

export function Reviewer({ onCount, targetPhotoId }) {
  const [summary, setSummary] = useState(null);
  const [sort, setSort] = useState("newest");
  const [ids, setIds] = useState([]);
  const [idx, setIdx] = useState(0);
  const [item, setItem] = useState(null);
  const [choices, setChoices] = useState([]);
  const [busy, setBusy] = useState(false);
  const [jobs, setJobs] = useState({ applying: 0, processing: 0 });
  const [goto, setGoto] = useState("");
  const seq = useRef(0);
  const current = useRef(null);

  const loadSummary = () => api("/api/review/summary").then((s) => { setSummary(s); onCount?.(s.to_review); });
  const processIfNew = (id) => api(`/api/review/items/${id}/process`, { method: "POST", params: { if_new: true } }).catch(() => {});
  const loadItem = async (id, list = ids, at = idx) => {
    if (!id) { setItem(null); current.current = null; return; }
    current.current = id;
    let it = await api(`/api/review/items/${id}`);
    if (it.status === "new") { await processIfNew(id); it = { ...it, status: "processing" }; }
    if (current.current !== id) return;           // moved on meanwhile
    setItem(it); setChoices(initialChoices(it));
    for (const next of list.slice(at + 1, at + 3)) processIfNew(next);   // get the next ones ready
  };
  useEffect(() => { loadSummary(); }, []);
  useEffect(() => {
    api("/api/review/queue", { params: { sort } }).then((list) => {
      const targetAt = targetPhotoId ? list.indexOf(targetPhotoId) : -1;
      const queue = targetPhotoId && targetAt < 0 ? [targetPhotoId, ...list] : list;
      const at = targetPhotoId ? (targetAt < 0 ? 0 : targetAt) : 0;
      setIds(queue); setIdx(at); loadItem(queue[at], queue, at);
    });
  }, [sort, targetPhotoId]);

  // Background work: outcomes of approvals and processing.
  useEffect(() => {
    const poll = async () => {
      try {
        const j = await api("/api/review/jobs", { params: { since: seq.current } });
        seq.current = j.seq; setJobs(j);
        for (const f of j.finished) {
          if (f.kind === "sync") continue;
          if (f.kind === "replan") { toast("Re-planned everything in review"); loadItem(current.current); continue; }
          if (f.status === "failed") toast(`⚠ ${f.title}: ${f.error || f.failed + " change(s) failed"}${f.kind === "apply" ? " (still in review)" : ""}`);
          else if (f.kind === "process" && f.photo_id === current.current) toast(`Processed ${f.title}`);
          if (f.photo_id === current.current) loadItem(f.photo_id);
        }
        if (j.finished.length) loadSummary();
      } catch { /* offline for a moment */ }
    };
    return every(poll, 1500);
  }, []);

  const move = (d) => { if (!ids.length) return; const n = (idx + d + ids.length) % ids.length; setIdx(n); loadItem(ids[n], ids, n); };
  const removeCurrent = () => {
    const rest = ids.filter((_, i) => i !== idx); const n = Math.min(idx, Math.max(0, rest.length - 1));
    setIds(rest); setIdx(n); loadItem(rest[n], rest, n);
  };
  const apply = async () => {
    if (!item || busy) return; setBusy(true);
    const payload = choices.map((ch, i) => item.changes[i].action === "set_tags"
      ? ((tags) => ({ approve: tags.length > 0 && !sameTags(tags, ch.old.map((x) => x.t)), value: tags }))(finalTags(ch))
      : item.changes[i].action === "set_location" ? { approve: ch.approve && !!ch.value, value: ch.value }
      : { approve: ch.approve, value: ch.value });
    try {
      await api(`/api/review/items/${item.photo_id}/decide`, { method: "POST", body: { choices: payload } });
      removeCurrent(); loadSummary();
    } catch (e) { toast("Error: " + e.message); }
    setBusy(false);
  };
  const ignore = async () => {
    if (!item || busy) return; setBusy(true);
    try {
      const r = await api(`/api/review/items/${item.photo_id}/ignore`, { method: "POST" });
      if (r.status === "busy") toast("It's being applied or processed; try again in a moment.");
      else { toast("Ignored: out of review, nothing changed"); removeCurrent(); loadSummary(); }
    } catch (e) { toast("Error: " + e.message); }
    setBusy(false);
  };
  const send = async () => {
    if (!item || busy) return; setBusy(true);
    try { await api(`/api/review/items/${item.photo_id}/send`, { method: "POST" }); loadItem(item.photo_id); loadSummary(); }
    catch (e) { toast("Error: " + e.message); }
    setBusy(false);
  };
  const processNow = async () => {
    if (!item || busy) return; setBusy(true);
    try {
      await api(`/api/review/items/${item.photo_id}/process`, { method: "POST" });
      toast("Processing in the background…");
      loadItem(item.photo_id);
    }
    catch (e) { toast("Error: " + e.message); }
    setBusy(false);
  };
  const find = async (e) => {
    e.preventDefault(); if (!goto.trim()) return;
    try {
      const { photo_id } = await api("/api/review/find", { params: { q: goto.trim() } });
      const at = ids.indexOf(photo_id);
      if (at >= 0) { setIdx(at); } else { setIds([...ids.slice(0, idx), photo_id, ...ids.slice(idx)]); }
      setGoto(""); loadItem(photo_id);
    } catch (err) { toast(err.message); }
  };

  const inReview = item && ["new", "pending", "processing", "applying"].includes(item.status);
  const ready = item && item.status === "pending";
  const editable = ready && item.changes.length > 0;
  useEffect(() => {
    const key = (e) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
      if (e.key === "Enter" && (!typing || e.metaKey || e.ctrlKey) && editable) { e.preventDefault(); apply(); }
      else if (typing) return;
      else if (e.key === "i" && ready) ignore();
      else if (e.key === "p" && ready) processNow();
      else if (e.key === "ArrowRight" || e.key === "s") move(1);
      else if (e.key === "ArrowLeft") move(-1);
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  });

  const p = item?.photo;
  const working = jobs.applying + jobs.processing;
  return html`
    <div class="toolbar">
      <${ReviewViews} />
      <b>${summary ? `${num(summary.to_review)} in review` : "In review"}</b>
      <select value=${sort} onChange=${(e) => setSort(e.target.value)} aria-label="Sort">
        <option value="newest">Newest first</option><option value="oldest">Oldest first</option>
        <option value="views">Most viewed</option><option value="faves">Most faved</option><option value="comments">Most commented</option>
      </select>
      <form onSubmit=${find}><input value=${goto} onInput=${(e) => setGoto(e.target.value)} placeholder="Go to photo ID or Flickr URL" aria-label="Go to photo" /></form>
      <span class="muted">${ids.length ? `${idx + 1} of ${num(ids.length)}` : ""}</span>
      <span class="spacer"></span>
      ${working > 0 && html`<a class="status s-applying" href="#/queue" title="Open the queue"><span class="spin">⟳</span> ${[jobs.applying && `applying ${jobs.applying}`, jobs.processing && `processing ${jobs.processing}`].filter(Boolean).join(" · ")}</a>`}
      <span class="muted small hint-keys"><kbd>Enter</kbd> apply · <kbd>I</kbd> ignore · <kbd>P</kbd> re-process · <kbd>→</kbd> skip · <kbd>←</kbd> back</span>
    </div>
    ${!item ? html`<div class="empty">Nothing in review. Send photos from <a href="#/photos">Photos</a> (open one, then Send to Review);
      new uploads arrive here after each sync.</div>` : html`<div class="review">
      <div class="pic">
        <img src=${p.image} alt="" onError=${(e) => { if (e.target.src !== p.thumb) e.target.src = p.thumb; }} />
        <div class="cap"><a href=${p.url} target="_blank" rel="noopener noreferrer">Open on Flickr ↗</a>
          <span>${(p.date_taken || "").slice(0, 10)}</span><span>${p.camera}</span>${p.media === "video" && html`<span class="status">video</span>`}</div>
      </div>
      <div class="panel">
        <div class="card"><h2>Proposed changes <span class=${"status s-" + item.status}>${STATE[item.status] || item.status}</span></h2>
          ${ready && item.result?.error && html`<p class="problem">⚠ Applying failed twice, so it's still here: ${item.result.error}.
            Apply to try again, or Re-process for a fresh proposal.</p>`}
          ${item.changes.map((c, i) => html`<${Change} key=${i} c=${c} ch=${choices[i]} i=${i} editable=${editable}
              out=${item.result?.changes?.[i]} onChange=${(k, v) => setChoices(choices.map((x, j) => (j === k ? v : x)))} />`)}
          ${item.status === "not_queued" && html`<p class="why">Not in review. Send it to review to propose its changes.</p>`}
          ${item.status === "new" && html`<p class="why">Waiting to be processed; processing it now…</p>`}
          ${item.status === "processing" && html`<p class="why"><span class="spin">⟳</span> Processing in the background…</p>`}
          ${item.status === "applying" && html`<p class="why"><span class="spin">⟳</span> Applying in the background…</p>`}
          ${ready && !item.changes.length && html`<p class="why">Nothing to change. Ignore it, or Re-process it.</p>`}
        </div>
        <div class="actions">
          ${editable && html`<button class="primary" onClick=${apply} disabled=${busy}>Apply</button>`}
          ${ready && html`<button onClick=${ignore} disabled=${busy} title="Out of review; nothing changes on Flickr">Ignore</button>
            <button onClick=${processNow} disabled=${busy} title="A fresh look; it stays in review">Re-process</button>`}
          ${item.status === "not_queued" && html`<button class="primary" onClick=${send} disabled=${busy}>Send to review</button>`}
          <button onClick=${() => move(1)}>${inReview ? "Skip" : "Next"}</button>
        </div>
        <div class="card"><h2>On Flickr now</h2><dl class="kv">
          <dt>Title</dt><dd>${p.title || html`<span class="muted">(blank)</span>`}</dd>
          <dt>Description</dt><dd>${plain(p.description) || html`<span class="muted">(blank)</span>`}</dd>
          <dt>Albums</dt><dd class="chips">${p.albums.length ? (p.album_links || p.albums.map((t) => ({ title: t }))).map((a) => a.id
            ? html`<a class="chip" href=${photosLink({ album: a.id })} target="_blank" rel="noopener" title="Open the album (new tab)">${a.title}</a>`
            : html`<span class="chip">${a.title}</span>`) : html`<span class="muted">none</span>`}</dd>
          <dt>Tags</dt><dd class="chips">${p.tags.length ? p.tags.map((t) => html`<a class="chip" href=${photosLink({ tag: t })} target="_blank"
            rel="noopener" title=${`All photos tagged ${t} (new tab)`}>${t}</a>`) : html`<span class="muted">none</span>`}</dd>
        </dl></div>
        <${Engagement} s=${item.stats} />
      </div>
    </div>`}`;
}
