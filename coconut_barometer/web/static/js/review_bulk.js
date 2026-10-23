// Review's other two views (#/photos/review/<bulk|applied>). Bulk: one kind of proposed change across every
// ready photo, approved or dropped many at a time (each photo's other changes stay in review), plus re-planning
// everything in review with the current settings. Applied: the latest applies, each undoable.
import { html, useState, useEffect, api, num, ago, plain, toast, every, go } from "./lib.js";

const VIEWS = [["", "One by one"], ["bulk", "Bulk"], ["applied", "Applied"]];
export function ReviewViews({ at = "" }) {
  return html`<span class="seg">${VIEWS.map(([v, label]) => html`<button class=${v === at ? "primary" : ""}
    onClick=${() => go("photos/review" + (v ? "/" + v : ""))}>${label}</button>`)}</span>`;
}

const KINDS = [["location", "Locations"], ["tags", "Tags"], ["title", "Titles"], ["description", "Descriptions"],
               ["album", "Albums"], ["group", "Groups"]];
const GROUP_LABEL = { location: "Confidence", album: "Album", group: "Group" };
const norm = (t) => t.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9:=]+/g, "");
const key = (i) => `${i.photo_id}:${i.index}`;
const placed = (c) => c.action !== "set_location" || (c.new && c.new.lat != null);

function Summary({ c }) {
  if (c.action === "set_location")
    return placed(c) ? html`<b>${c.new.place || `${c.new.lat.toFixed(3)}, ${c.new.lon.toFixed(3)}`}</b>
        <span class=${"status conf-" + c.confidence}>${c.confidence}</span><div class="why clamp">${c.reason}</div>`
      : html`<span class="muted">No guess: pick it in One by one</span>`;
  if (c.action === "set_tags") {
    const had = new Set((c.old || "").split(/\s+/).filter(Boolean).map(norm));
    const kept = new Set(c.new.map(norm));
    const fresh = c.new.filter((t) => !had.has(norm(t))), gone = [...had].filter((t) => !kept.has(t));
    return html`<span class="up">+${fresh.length}</span> <span class="down">−${gone.length}</span>
      <div class="chips mt-1">${fresh.slice(0, 8).map((t) => html`<span class="chip prop fresh">${t}</span>`)}
        ${gone.slice(0, 6).map((t) => html`<span class="chip gone">${t}</span>`)}</div>`;
  }
  if (c.action === "set_title") return html`<b>${c.new}</b><div class="why">was: ${c.old || "(blank)"}</div>`;
  if (c.action === "set_description") return html`<div class="clamp">${plain(c.new)}</div>`;
  return html`<b>${c.new}</b>${c.action === "add_to_album" && !c.album_id ? html` <span class="why">(new album)</span>` : ""}
    ${c.reason && html`<div class="why clamp">${c.reason}</div>`}`;
}

export function BulkReview() {
  const [kind, setKind] = useState("location");
  const [value, setValue] = useState("");
  const [d, setD] = useState(null);
  const [sel, setSel] = useState(new Set());
  const [busy, setBusy] = useState(false);
  const load = () => api("/api/review/bulk", { params: { kind, value } }).then((r) => { setD(r); setSel(new Set()); });
  useEffect(() => { setD(null); load(); }, [kind, value]);
  // Photos still being processed (or applied) aren't listed yet: look again until they're done, unless some are picked.
  const busyCount = d?.busy || 0;
  useEffect(() => {
    if (!busyCount || sel.size) return;
    const t = setTimeout(() => load().catch(() => {}), 5000);
    return () => clearTimeout(t);
  }, [d, sel.size]);
  const items = d?.items || [];
  const chosen = items.filter((i) => sel.has(key(i)));
  const toggle = (i) => { const s = new Set(sel); s.has(key(i)) ? s.delete(key(i)) : s.add(key(i)); setSel(s); };
  const act = async (verb) => {
    const picks = chosen.filter((i) => verb === "drop" || placed(i.change))
      .map((i) => ({ photo_id: i.photo_id, index: i.index, action: i.change.action }));
    if (!picks.length || busy) return;
    if (verb === "apply" && kind === "tags" && !confirm(`Replace the tags of ${picks.length} photo(s) with their proposed lists?`)) return;
    setBusy(true);
    try {
      const r = await api("/api/review/bulk", { method: "POST", body: { verb, picks } });
      toast(verb === "apply" ? `Applying ${r.changes} change(s) in the background (undo under Applied)`
        : `Dropped ${r.changes} proposal(s)`);
      if (r.skipped) toast(`${r.skipped} skipped: changed since they were listed`);
      await load();
    } catch (e) { toast("Error: " + e.message); }
    setBusy(false);
  };
  const replan = async () => {
    try {
      const r = await api("/api/review/replan", { method: "POST" });
      toast(r.status === "busy" ? "Already re-planning" : "Re-planning everything in review with the current settings…");
    } catch (e) { toast("Error: " + e.message); }
  };
  const applicable = chosen.filter((i) => placed(i.change)).length;
  return html`
    <div class="toolbar"><${ReviewViews} at="bulk" /><span class="spacer"></span>
      <button onClick=${replan} title="Plan every photo in review again with the current settings (tags to always add or remove, ...). No vision or Flickr calls.">Re-plan all in review</button></div>
    <div class="toolbar"><span class="seg">${KINDS.map(([k, label]) => html`<button class=${kind === k ? "primary" : ""}
        onClick=${() => { setKind(k); setValue(""); }}>${label}${d?.counts?.[k] ? html` <span class="muted">${num(d.counts[k])}</span>` : ""}</button>`)}</span></div>
    ${d && GROUP_LABEL[kind] && Object.keys(d.groups).length > 0 && html`<div class="toolbar">
      <select value=${value} onChange=${(e) => setValue(e.target.value)} aria-label=${GROUP_LABEL[kind]}>
        <option value="">${GROUP_LABEL[kind]}: all</option>
        ${Object.entries(d.groups).map(([g, n]) => html`<option value=${g}>${g} (${num(n)})</option>`)}</select></div>`}
    ${d?.busy > 0 && html`<p class="muted small"><span class="spin">⟳</span> ${num(d.busy)} photo${d.busy === 1 ? "" : "s"} still being
      processed or applied: listed here when done.</p>`}
    ${!d ? html`<div class="boot">Loading…</div>` : !items.length ? html`<div class="empty">Nothing of this kind waiting.</div>` : html`
      <div class="toolbar actbar-bulk">
        <label class="check"><input type="checkbox" checked=${chosen.length === items.length}
          onChange=${(e) => setSel(e.target.checked ? new Set(items.map(key)) : new Set())} /> all ${num(items.length)}${d.total > items.length ? ` of ${num(d.total)}` : ""}</label>
        <span class="spacer"></span>
        <button class="primary" disabled=${busy || !applicable} onClick=${() => act("apply")}>Apply ${applicable || ""}</button>
        <button disabled=${busy || !chosen.length} onClick=${() => act("drop")} title="Drop these proposals; nothing changes on Flickr">Drop ${chosen.length || ""}</button>
      </div>
      <div class="bulkgrid">${items.map((i) => html`<div key=${key(i)} class=${"bulkcard" + (sel.has(key(i)) ? " on" : "")}
          onClick=${(e) => { if (!e.target.closest("a")) toggle(i); }}>
        <img src=${i.thumb} alt="" loading="lazy" />
        <div class="body"><div class="t"><input type="checkbox" checked=${sel.has(key(i))} aria-label="Select" />
          <a href=${"#/photos/review?photo_id=" + i.photo_id} title="Review this photo one by one">${i.title || "Untitled"}</a></div>
          <${Summary} c=${i.change} /></div></div>`)}</div>`}`;
}

export function AppliedList() {
  const [rows, setRows] = useState(null);
  const load = () => api("/api/review/applied").then(setRows).catch(() => {});
  useEffect(() => every(load, 4000), []);
  const undo = async (r) => {
    if (!confirm(`Undo what was applied to “${r.title}”? (${r.what.join(", ")})`)) return;
    try { await api(`/api/review/items/${r.photo_id}/undo`, { method: "POST" }); toast("Undoing in the background…"); load(); }
    catch (e) { toast("Error: " + e.message); }
  };
  return html`<div class="toolbar"><${ReviewViews} at="applied" /></div>
    ${!rows ? html`<div class="boot">Loading…</div>` : !rows.length ? html`<div class="empty">Nothing applied yet.</div>` : html`
      <div class="card"><h2>Applied</h2>
        <p class="muted small">Undo puts back what an apply replaced: the title, description, tags, album, location or
          group it changed. Anything changed on Flickr since is left as it is. A photo's latest apply can be undone.</p>
        ${rows.map((r) => html`<div class="applied" key=${r.id}>
          <img src=${r.thumb} alt="" loading="lazy" />
          <div class="body"><a href=${"#/photos/review?photo_id=" + r.photo_id}>${r.title}</a>
            <div class="why">${ago(r.at)} · ${r.what.join(" · ")}</div>
            ${r.undone_at && html`<div class="why">Undone ${ago(r.undone_at)}${r.result?.skipped?.length ? ` · ${r.result.skipped.join("; ")}` : ""}</div>`}</div>
          ${r.undoing ? html`<span class="status s-applying"><span class="spin">⟳</span> undoing</span>`
            : r.can_undo ? html`<button onClick=${() => undo(r)}>Undo</button>` : ""}
        </div>`)}</div>`}`;
}
