"""Self-contained HTML page for reviewing a plan visually.

Click a thumbnail to reject/approve it, click a tag chip to drop/keep that tag,
then "Download reviewed CSV" and pass that file to `apply`.
"""
from __future__ import annotations

import json
from dataclasses import asdict

from .planner import PLAN_FIELDS

TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flickr Plan Review</title>
<style>
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--card:#fff;--line:#e7e5e4;--accent:#0063dc;--no:#dc2626}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--fg:#f5f5f4;--muted:#a8a29e;--card:#292524;--line:#44403c;--accent:#4d9fff;--no:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:12px;flex-wrap:wrap;align-items:center}
h1{font-size:17px;margin:0 12px 0 0}button,select{font:inherit;padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.stat{color:var(--muted)}main{padding:16px;max-width:1400px;margin:auto}
section{margin:0 0 28px}section h2{font-size:15px;margin:0 0 8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
section h2 small{color:var(--muted);font-weight:400}section h2 button{padding:2px 8px;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.tile{position:relative;background:var(--card);border:2px solid var(--line);border-radius:8px;overflow:hidden;cursor:pointer}
.tile img{width:100%;aspect-ratio:1;object-fit:cover;display:block}
.tile .meta{padding:4px 6px;font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile.off{border-color:var(--no);opacity:.45}.tile.off::after{content:"✕";position:absolute;top:4px;right:8px;color:var(--no);font-weight:700;font-size:18px}
.tile a{position:absolute;top:4px;left:4px;background:#000a;color:#fff;font-size:11px;padding:1px 5px;border-radius:4px;text-decoration:none}
.tagrow{display:flex;gap:10px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px;margin-bottom:6px}
.tagrow img{width:72px;height:72px;object-fit:cover;border-radius:4px;flex:none}
.chips{display:flex;gap:6px;flex-wrap:wrap}.chip{padding:2px 9px;border-radius:999px;border:1px solid var(--accent);color:var(--accent);cursor:pointer;font-size:12px;user-select:none}
.chip.off{border-color:var(--no);color:var(--no);text-decoration:line-through;opacity:.6}
.ttl{font-size:12px;color:var(--muted)}
.tagrow.off{border-color:var(--no);opacity:.5}.tagrow.off .rename{text-decoration:line-through}
.rename{font:inherit;width:100%;max-width:420px;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
textarea.rename{max-width:720px;resize:vertical}
</style></head><body>
<header><h1>Plan review</h1>
<select id="filter"><option value="">All sources</option></select>
<span class="stat" id="stat"></span>
<span style="flex:1"></span>
<button class="primary" id="dl">Download reviewed CSV</button></header>
<main id="main"></main>
<script>
const FIELDS = __FIELDS__;
const ROWS = __ROWS__;
const PLAN_NAME = __NAME__;
ROWS.forEach(r => { r.approve = 'y'; if (r.action === 'add_tags') r.tagState = r.target.split(' | ').map(t => ({t, on: true})); });
const main = document.getElementById('main'), filter = document.getElementById('filter');
const sources = [...new Set(ROWS.map(r => r.action === 'add_tags' ? 'tags' : r.source))].sort();
sources.forEach(s => filter.add(new Option(s, s)));
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function render() {
  const f = filter.value; main.innerHTML = '';
  const groups = new Map();
  ROWS.filter(r => r.action === 'add_to_album' && (!f || r.source === f)).forEach(r => {
    if (!groups.has(r.target)) groups.set(r.target, []); groups.get(r.target).push(r); });
  for (const [title, rows] of groups) {
    const sec = document.createElement('section');
    const isNew = !rows[0].album_id;
    sec.innerHTML = `<h2>${esc(title)} <small>${rows.length} photo(s) · ${esc(rows[0].source)}${isNew ? ' · NEW ALBUM' : ''}</small>
      <button data-all="y">approve all</button><button data-all="n">reject all</button></h2><div class="grid"></div>`;
    const grid = sec.querySelector('.grid');
    rows.forEach(r => {
      const d = document.createElement('div'); d.className = 'tile' + (r.approve === 'y' ? '' : ' off');
      d.title = `${r.photo_title}\n${r.reason}${r.confidence ? ' (' + r.confidence + ')' : ''}`;
      d.innerHTML = `<img loading="lazy" src="${esc(r.thumb_url)}" alt=""><a href="${esc(r.photo_url)}" target="_blank" rel="noopener">open</a>
        <div class="meta">${esc(r.confidence ? r.confidence + ' · ' : '')}${esc(r.reason || r.photo_title)}</div>`;
      d.onclick = e => { if (e.target.tagName === 'A') return; r.approve = r.approve === 'y' ? 'n' : 'y'; d.classList.toggle('off'); stat(); };
      grid.appendChild(d);
    });
    sec.querySelectorAll('[data-all]').forEach(b => b.onclick = () => { rows.forEach(r => r.approve = b.dataset.all); render(); });
    main.appendChild(sec);
  }
  editSection('set_title', 'geo:rename', 'Renames', 'edit the new title', r => `${r.original_target || '(blank)'} →`, false);
  editSection('set_description', 'vision:describe', 'Descriptions', 'edit the new description',
              r => r.photo_title || '(untitled)', true);
  const tagRows = ROWS.filter(r => r.action === 'add_tags');
  if (tagRows.length && (!f || f === 'tags')) {
    const sec = document.createElement('section');
    sec.innerHTML = `<h2>Tags <small>${tagRows.length} photo(s) · click a tag to drop it</small></h2>`;
    tagRows.forEach(r => {
      const d = document.createElement('div'); d.className = 'tagrow';
      d.innerHTML = `<a href="${esc(r.photo_url)}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(r.thumb_url)}" alt=""></a>
        <div><div class="ttl">${esc(r.photo_title || r.photo_id)}</div><div class="chips"></div></div>`;
      const chips = d.querySelector('.chips');
      r.tagState.forEach(ts => { const c = document.createElement('span'); c.className = 'chip' + (ts.on ? '' : ' off'); c.textContent = ts.t;
        c.onclick = () => { ts.on = !ts.on; c.classList.toggle('off'); stat(); }; chips.appendChild(c); });
      sec.appendChild(d);
    });
    main.appendChild(sec);
  }
  stat();
}
// Rows with free-text targets (titles, descriptions): editable field plus a skip toggle.
function editSection(action, source, heading, hint, label, multiline) {
  const rows = ROWS.filter(r => r.action === action);
  if (!rows.length || (filter.value && filter.value !== source)) return;
  const sec = document.createElement('section');
  sec.innerHTML = `<h2>${heading} <small>${rows.length} photo(s) · ${hint}, or click ✕ to skip</small>
    <button data-all="y">approve all</button><button data-all="n">reject all</button></h2>`;
  rows.forEach(r => {
    const d = document.createElement('div'); d.className = 'tagrow' + (r.approve === 'y' ? '' : ' off');
    const field = multiline ? `<textarea class="rename" rows="2">${esc(r.target)}</textarea>`
                            : `<input class="rename" value="${esc(r.target)}">`;
    d.innerHTML = `<a href="${esc(r.photo_url)}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(r.thumb_url)}" alt=""></a>
      <div style="flex:1;min-width:0"><div class="ttl">${esc(label(r))}</div>${field}</div><button class="x" title="skip">✕</button>`;
    d.querySelector('.rename').oninput = e => { r.target = e.target.value; };
    d.querySelector('.x').onclick = () => { r.approve = r.approve === 'y' ? 'n' : 'y'; d.classList.toggle('off'); stat(); };
    sec.appendChild(d);
  });
  sec.querySelectorAll('[data-all]').forEach(b => b.onclick = () => { rows.forEach(r => r.approve = b.dataset.all); render(); });
  main.appendChild(sec);
}
function finalRows() {
  return ROWS.map(r => {
    if (r.action !== 'add_tags') return r;
    const kept = r.tagState.filter(t => t.on).map(t => t.t);
    return {...r, target: kept.join(' | '), approve: kept.length ? 'y' : 'n'};
  });
}
function stat() {
  const rows = finalRows(); const yes = rows.filter(r => r.approve === 'y').length;
  document.getElementById('stat').textContent = `${yes} approved · ${rows.length - yes} rejected · ${rows.length} total`;
}
document.getElementById('dl').onclick = () => {
  const q = v => { v = String(v ?? ''); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  const csv = [FIELDS.join(','), ...finalRows().map(r => FIELDS.map(k => q(r[k])).join(','))].join('\n') + '\n';
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([csv], {type: 'text/csv'}));
  a.download = PLAN_NAME.replace(/\.csv$/, '') + '-reviewed.csv'; a.click();
};
filter.onchange = render; render();
</script></body></html>"""


def _js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def write_report(rows, path: str, plan_name: str) -> None:
    data = [{k: asdict(r)[k] for k in PLAN_FIELDS} for r in rows]
    html = (TEMPLATE.replace("__FIELDS__", _js(PLAN_FIELDS))
                    .replace("__ROWS__", _js(data))
                    .replace("__NAME__", _js(plan_name)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
