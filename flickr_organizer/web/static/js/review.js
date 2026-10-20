// The reviewer: one photo at a time, approve each proposed change. Approvals and
// processing run in the server's background worker; this page polls for outcomes.
import { html, useState, useEffect, useRef, api, num, day, plain, toast, photosLink } from "./lib.js";

// "To review" also holds new photos, processed when you reach them ("new").
const LABELS = { pending: "To review", applying: "Applying", failed: "Failed", applied: "Applied", rejected: "Rejected",
                 none: "Nothing to change", unprocessed: "Not processed" };

const TAGS = new Set(["add_tags", "set_tags"]);
// Flickr's normalized tag form (as in util.norm_tag), to tell which existing tags a replacement drops.
const norm = (t) => t.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9:=]+/g, "");

function initialChoices(item) {
  return item.changes.map((c) => ({ approve: true, value: c.new,
    tags: TAGS.has(c.action) ? c.new.map((t) => ({ t, on: true })) : null }));
}

function Change({ c, ch, i, editable, out, onChange }) {
  const why = [c.source, c.confidence, c.reason].filter(Boolean).join(" · ");
  const status = out && out.status ? html` <span class=${"status s-" + out.status}>${out.status}</span>` : "";
  const set = (patch) => onChange(i, { ...ch, ...patch });
  if (TAGS.has(c.action)) {
    const srcs = c.source.split(",").map((s) => s.replace(/^(std|vision|geo):/, "")).join(", ");
    const kept = new Set(ch.tags.filter((x) => x.on).map((x) => norm(x.t)));
    const dropped = c.action === "set_tags" ? (c.old || "").split(/\s+/).filter((t) => t && !kept.has(norm(t))) : [];
    return html`<div class="row"><div class="what">${c.action === "set_tags" ? html`<b>Replace all tags</b> with` : "Add tags"} <span class="why">· ${srcs}${editable ? " · click a tag to drop it" : ""}</span>
      ${dropped.length > 0 && html`<div class="chips removed" style="margin-top:6px" title="Removed from the photo on approval">
        <span class="why">removes:</span>${dropped.map((t) => html`<span class="chip gone">${t}</span>`)}</div>`}
      <div class="chips" style="margin-top:6px">${ch.tags.map((ts, j) => {
        const st = out?.tags?.[ts.t];
        return html`<button type="button" class=${"chip " + (st || "prop" + (ts.on ? "" : " off"))} disabled=${!editable}
          aria-pressed=${ts.on} onClick=${() => set({ tags: ch.tags.map((x, k) => (k === j ? { ...x, on: !x.on } : x)) })}>${ts.t}</button>`;
      })}</div></div></div>`;
  }
  const box = html`<input type="checkbox" checked=${ch.approve} disabled=${!editable} aria-label="Approve"
                          onChange=${(e) => set({ approve: e.target.checked })} />`;
  if (c.action === "add_to_album")
    return html`<div class="row">${box}<div class="what">Add to album <b>${c.new}</b>${c.album_id ? "" : " (new album)"}${status}
      <div class="why">${why}</div></div></div>`;
  if (c.action === "set_title")
    return html`<div class="row">${box}<div class="what">Title${status} <span class="why">was: ${c.old || "(blank)"}</span>
      <input type="text" value=${ch.value} disabled=${!editable} onInput=${(e) => set({ value: e.target.value })} /></div></div>`;
  if (c.action === "set_description")
    return html`<div class="row">${box}<div class="what">Description${status}
      ${c.old && html`<div class="replaces">replaces: ${plain(c.old)}</div>`}
      <textarea value=${ch.value} disabled=${!editable} onInput=${(e) => set({ value: e.target.value })}></textarea></div></div>`;
  return html`<div class="row"><div class="what">${c.action}: ${String(c.new)}</div></div>`;
}

function Engagement({ s }) {
  if (!s) return null;
  const growth = s.first_snapshot && s.views != null && s.views > s.first_snapshot.views;
  return html`<div class="card"><h2>Engagement</h2>
    <div class="kpis" style="margin:0 0 10px">
      ${[["views", s.views], ["faves", s.faves], ["comments", s.comments], ["groups", s.groups.length]].map(([k, v]) =>
        html`<div class="kpi"><span>${k}</span><b>${num(v)}</b></div>`)}
    </div>
    <p class="muted" style="margin:0 0 8px">${[growth && `+${num(s.views - s.first_snapshot.views)} views since ${s.first_snapshot.day}`,
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

export function Reviewer({ onCount }) {
  const [summary, setSummary] = useState(null);
  const [status, setStatus] = useState("pending");
  const [sort, setSort] = useState("newest");
  const [ids, setIds] = useState([]);
  const [idx, setIdx] = useState(0);
  const [item, setItem] = useState(null);
  const [choices, setChoices] = useState([]);
  const [busy, setBusy] = useState(false);
  const [jobs, setJobs] = useState({ applying: 0, processing: 0 });
  const [goto, setGoto] = useState("");
  const [tagMode, setTagMode] = useState("add");       // when (re-)processing: add to tags, or replace them
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
    api("/api/review/queue", { params: { status, sort } }).then((list) => { setIds(list); setIdx(0); loadItem(list[0], list, 0); });
  }, [status, sort]);

  // Background work: outcomes of approvals and processing.
  useEffect(() => {
    const poll = async () => {
      try {
        const j = await api("/api/review/jobs", { params: { since: seq.current } });
        seq.current = j.seq; setJobs(j);
        for (const f of j.finished) {
          if (f.kind === "sync") continue;
          if (f.status === "failed") toast(`⚠ ${f.title}: ${f.error || f.failed + " change(s) failed"}`);
          else if (f.kind === "process" && f.photo_id === current.current) toast(`Processed ${f.title}: ${f.status === "pending" ? "changes ready" : "nothing to change"}`);
          if (f.photo_id === current.current) loadItem(f.photo_id);
        }
        if (j.finished.length) loadSummary();
      } catch { /* offline for a moment */ }
    };
    poll(); const t = setInterval(poll, 1500); return () => clearInterval(t);
  }, []);

  const move = (d) => { if (!ids.length) return; const n = (idx + d + ids.length) % ids.length; setIdx(n); loadItem(ids[n], ids, n); };
  const removeCurrent = () => {
    const rest = ids.filter((_, i) => i !== idx); const n = Math.min(idx, Math.max(0, rest.length - 1));
    setIds(rest); setIdx(n); loadItem(rest[n], rest, n);
  };
  const decide = async (approve) => {
    if (!item || busy) return; setBusy(true);
    const payload = choices.map((ch, i) => TAGS.has(item.changes[i].action)
      ? { approve: approve && ch.tags.some((t) => t.on), value: ch.tags.filter((t) => t.on).map((t) => t.t) }
      : { approve: approve && ch.approve, value: ch.value });
    try {
      await api(`/api/review/items/${item.photo_id}/decide`, { method: "POST", body: { choices: payload } });
      if (!approve) toast("Rejected — won't be proposed again");
      removeCurrent(); loadSummary();
    } catch (e) { toast("Error: " + e.message); }
    setBusy(false);
  };
  const processNow = async () => {
    if (!item || busy) return; setBusy(true);
    try {
      await api(`/api/review/items/${item.photo_id}/process`, { method: "POST", params: { tags: tagMode } });
      toast(tagMode === "replace" ? "Processing; its tags will be proposed as a replacement…" : "Processing in the background…");
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

  const editable = item && (item.status === "pending" || item.status === "failed") && item.changes.length > 0;
  useEffect(() => {
    const key = (e) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
      if (e.key === "Enter" && (!typing || e.metaKey || e.ctrlKey) && editable) { e.preventDefault(); decide(true); }
      else if (typing) return;
      else if (e.key === "r" && editable) decide(false);
      else if (e.key === "p" && item && !["processing", "applying"].includes(item.status)) processNow();
      else if (e.key === "ArrowRight" || e.key === "s") move(1);
      else if (e.key === "ArrowLeft") move(-1);
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  });

  const p = item?.photo;
  const busyNow = item && ["processing", "applying"].includes(item.status);
  const working = jobs.applying + jobs.processing;
  return html`
    <div class="toolbar">
      <select value=${status} onChange=${(e) => setStatus(e.target.value)} aria-label="Show">
        ${Object.entries(LABELS).map(([k, label]) => html`<option value=${k}>${label}${summary ? ` (${num(k === "pending" ? summary.to_review : summary[k])})` : ""}</option>`)}
      </select>
      <select value=${sort} onChange=${(e) => setSort(e.target.value)} aria-label="Sort">
        <option value="newest">Newest first</option><option value="oldest">Oldest first</option>
        <option value="views">Most viewed</option><option value="faves">Most faved</option><option value="comments">Most commented</option>
      </select>
      <form onSubmit=${find}><input value=${goto} onInput=${(e) => setGoto(e.target.value)} placeholder="Go to photo ID or Flickr URL" aria-label="Go to photo" /></form>
      <span class="muted">${ids.length ? `${idx + 1} of ${num(ids.length)}` : ""}</span>
      <span class="spacer"></span>
      ${working > 0 && html`<a class="status s-applying" href="#/queue" title="Open the queue"><span class="spin">⟳</span> ${[jobs.applying && `applying ${jobs.applying}`, jobs.processing && `processing ${jobs.processing}`].filter(Boolean).join(" · ")}</a>`}
      <span class="muted" style="font-size:12px"><kbd>Enter</kbd> approve · <kbd>R</kbd> reject · <kbd>P</kbd> process · <kbd>→</kbd> skip · <kbd>←</kbd> back</span>
    </div>
    ${!item ? html`<div class="empty">Nothing here.</div>` : html`<div class="review">
      <div class="pic">
        <img src=${p.image} alt="" onError=${(e) => { if (e.target.src !== p.thumb) e.target.src = p.thumb; }} />
        <div class="cap"><a href=${p.url} target="_blank" rel="noopener noreferrer">Open on Flickr ↗</a>
          <span>${(p.date_taken || "").slice(0, 10)}</span><span>${p.camera}</span>${p.media === "video" && html`<span class="status">video</span>`}</div>
      </div>
      <div class="panel">
        <div class="card"><h2>Proposed changes <span class=${"status s-" + item.status}>${item.status}</span></h2>
          ${item.changes.map((c, i) => html`<${Change} key=${i} c=${c} ch=${choices[i]} i=${i} editable=${editable}
              out=${item.result?.changes?.[i]} onChange=${(k, v) => setChoices(choices.map((x, j) => (j === k ? v : x)))} />`)}
          ${item.status === "unprocessed" && html`<p class="why">Not processed yet. Process it to fetch EXIF, classify and plan its changes.</p>`}
          ${item.status === "new" && html`<p class="why">New from the last sync; processing it now…</p>`}
          ${item.status === "processing" && html`<p class="why"><span class="spin">⟳</span> Processing in the background…</p>`}
          ${!item.changes.length && !["unprocessed", "processing"].includes(item.status) && html`<p class="why">Nothing to change.</p>`}
        </div>
        <div class="actions">
          ${editable && html`<button class="primary" onClick=${() => decide(true)} disabled=${busy}>Approve selected</button>
            <button class="danger" onClick=${() => decide(false)} disabled=${busy}>Reject all</button>`}
          <button onClick=${() => move(1)}>${editable ? "Skip" : "Next"}</button>
          ${!busyNow && item.status !== "new" && html`<span class="procgroup">
            <button onClick=${processNow} disabled=${busy}>${item.status === "unprocessed" ? "Process now" : "Re-process"}</button>
            <select value=${tagMode} onChange=${(e) => setTagMode(e.target.value)} aria-label="Tags when processing"
                    title="What processing proposes for the photo's existing tags">
              <option value="add">Tags: add to existing</option>
              <option value="replace">Tags: overwrite existing</option>
            </select></span>`}
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
