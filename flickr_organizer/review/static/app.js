const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const api = async (path, body) => {
  const r = await fetch(path, {method: body ? 'POST' : 'GET', headers: {'X-Token': TOKEN, 'Content-Type': 'application/json'},
                               body: body ? JSON.stringify(body) : undefined});
  const data = await r.json(); if (!r.ok) throw new Error(data.error || r.status); return data;
};
let ids = [], idx = 0, item = null, choices = [], busy = false;

function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2600); }

async function loadSummary(keep) {
  const s = await api('/api/summary'); const sel = $('#status'); const cur = keep || sel.value || 'pending';
  const labels = {pending: 'To review', applying: 'Applying', failed: 'Failed', applied: 'Applied', rejected: 'Rejected',
                  none: 'Nothing to change', unprocessed: 'Not processed'};
  sel.innerHTML = Object.keys(labels).map(k => `<option value="${k}">${labels[k]} (${s[k]})</option>`).join('');
  sel.value = cur;
}
async function loadQueue() { ids = await api('/api/queue?status=' + $('#status').value + '&sort=' + $('#sort').value); idx = Math.min(idx, Math.max(0, ids.length - 1)); await show(); }

async function show() {
  const main = $('#main');
  $('#pos').textContent = ids.length ? `${idx + 1} of ${ids.length}` : '';
  if (!ids.length) { main.innerHTML = `<div class="empty">Nothing here. Run <code>python -m flickr_organizer process</code> to queue more photos.</div>`; item = null; loadSummary(); return; }
  item = await api('/api/item/' + ids[idx]);
  const p = item.photo, editable = item.status === 'pending' || item.status === 'failed';
  loadSummary();
  choices = item.changes.map(c => ({approve: true, value: c.new,
    tags: c.action === 'add_tags' ? c.new.map(t => ({t, on: true})) : null}));
  const res = item.result ? item.result.changes : null;
  const current = `<div class="box cur"><h2>On Flickr now</h2><dl>
      <dt>Title</dt><dd>${esc(p.title) || '<span class="muted">(blank)</span>'}</dd>
      <dt>Description</dt><dd>${esc(p.description) || '<span class="muted">(blank)</span>'}</dd>
      <dt>Albums</dt><dd class="chips">${p.albums.map(a => `<span class="chip">${esc(a)}</span>`).join('') || '<span class="muted">none</span>'}</dd>
      <dt>Tags</dt><dd class="chips">${p.tags.map(t => `<span class="chip">${esc(t)}</span>`).join('') || '<span class="muted">none</span>'}</dd>
    </dl></div>`;
  const rows = item.changes.map((c, i) => renderChange(c, i, editable, res && res[i])).join('');
  const busyNow = item.status === 'processing' || item.status === 'applying';
  const proc = busyNow ? '' : `<button id="proc">${item.status === 'unprocessed' ? 'Process now' : 'Re-process'} <kbd>P</kbd></button>`;
  const buttons = item.status === 'unprocessed'
    ? `<button class="primary" id="proc">Process now <kbd style="color:#fff;border-color:#fff8">P</kbd></button><button id="skip">Skip</button>`
    : editable && item.changes.length
    ? `<button class="primary" id="ok">Approve selected <kbd style="color:#fff;border-color:#fff8">Enter</kbd></button>
       <button class="danger" id="no">Reject all</button><button id="skip">Skip</button>${proc}`
    : `<span class="badge b-${item.status}">${busyNow ? '<span class="spin">⟳</span> ' : ''}${item.status}</span><button id="skip">Next</button>${proc}`;
  const empty = item.status === 'unprocessed' ? '<div class="why">Not processed yet. Process it to fetch EXIF, classify and plan its changes.</div>'
    : item.status === 'processing' ? '<div class="why"><span class="spin">⟳</span> Processing in the background…</div>'
    : !item.changes.length ? '<div class="why">Nothing to change.</div>' : '';
  main.innerHTML = `<div class="pic"><img src="${esc(p.image)}" alt="" onerror="this.onerror=null;this.src=${esc(JSON.stringify(p.thumb))}">
      <div class="cap"><a href="${esc(p.url)}" target="_blank" rel="noopener">Open on Flickr ↗</a>
      <span>${esc(p.date_taken || '')}</span><span>${esc(p.camera || '')}</span>${p.media === 'video' ? '<span class="badge">video</span>' : ''}</div></div>
    <div class="panel"><div class="box"><h2>Proposed changes <span class="badge b-${item.status}">${item.status}</span></h2>${rows}${empty}</div>
      <div class="actions">${buttons}</div>${current}${engagement(item.stats, p.url)}</div>`;
  main.querySelectorAll('[data-i]').forEach(el => {
    const i = +el.dataset.i, ch = choices[i];
    if (el.type === 'checkbox') el.onchange = () => { ch.approve = el.checked; };
    else if (el.dataset.t !== undefined) el.onclick = () => { if (!editable) return; const ts = ch.tags[+el.dataset.t]; ts.on = !ts.on; el.classList.toggle('off', !ts.on); };
    else el.oninput = () => { ch.value = el.value; };
  });
  if ($('#ok')) $('#ok').onclick = () => decide(true);
  if ($('#no')) $('#no').onclick = () => decide(false);
  if ($('#proc')) $('#proc').onclick = processNow;
  $('#skip').onclick = () => move(1);
}

const num = n => n == null ? '–' : Number(n).toLocaleString();
const day = ts => ts ? new Date(ts * 1000).toLocaleDateString(undefined, {year: 'numeric', month: 'short', day: 'numeric'}) : '';
function engagement(s, url) {
  if (!s) return '';
  const growth = s.first_snapshot && s.views != null && s.views > s.first_snapshot.views
    ? `<span class="up">+${num(s.views - s.first_snapshot.views)} views since ${esc(s.first_snapshot.day)}</span>` : '';
  const meta = [growth, s.license && esc(s.license), s.width && `${num(s.width)} × ${num(s.height)}`, s.uploaded && 'uploaded ' + day(s.uploaded)]
    .filter(Boolean).map(x => `<span>${x}</span>`).join('');
  const groups = s.groups.length
    ? `<dt>In ${s.groups.length} group${s.groups.length > 1 ? 's' : ''}</dt><dd class="chips">${s.groups.map(g =>
        `<a class="chip" href="https://www.flickr.com/groups/${esc(g.path_alias || g.id)}/" target="_blank" rel="noopener"
            title="${num(g.members)} members">${esc(g.name)}</a>`).join('')}</dd>` : '';
  const faves = s.recent_faves.length
    ? `<dt>Recently faved by</dt><dd class="chips">${s.recent_faves.map(f =>
        `<span class="chip" title="${esc(day(f.date_faved))}">${esc(f.real_name || f.username)}</span>`).join('')}</dd>`
    : s.faves && !s.faves_fetched ? `<dt>Faves</dt><dd class="muted">Run <code>social</code> to see who faved it</dd>` : '';
  const comments = s.recent_comments.length
    ? `<dt>Latest comments</dt><dd>${s.recent_comments.map(c => `<div class="cmt"><div class="who">${esc(c.real_name || c.author_name)} · ${esc(day(c.date_created))}</div>
        <div class="txt">${esc(c.text.replace(/<[^>]*>/g, ''))}</div></div>`).join('')}</dd>`
    : s.comments && !s.comments_fetched ? `<dt>Comments</dt><dd class="muted">Run <code>social</code> to load them</dd>` : '';
  return `<div class="box cur"><h2>Engagement</h2>
    <div class="tiles"><div class="tile"><b>${num(s.views)}</b><span>views</span></div><div class="tile"><b>${num(s.faves)}</b><span>faves</span></div>
      <div class="tile"><b>${num(s.comments)}</b><span>comments</span></div><div class="tile"><b>${num(s.groups.length)}</b><span>groups</span></div></div>
    <div class="meta">${meta}</div><dl>${groups}${faves}${comments}</dl></div>`;
}

function renderChange(c, i, editable, out) {
  const dis = editable ? '' : 'disabled';
  const why = esc([c.source, c.confidence, c.reason].filter(Boolean).join(' · '));
  if (c.action === 'add_tags') {
    const srcs = c.source.split(',').map(s => s.replace(/^(std|vision|geo):/, '')).join(', ');
    const chips = choices[i].tags.map((ts, j) => {
      const st = out && out.tags ? out.tags[ts.t] : null;
      return `<span class="chip ${st ? st : 'prop'}" data-i="${i}" data-t="${j}">${esc(ts.t)}</span>`;
    }).join('');
    return `<div class="row"><div class="what"><div>Add tags <span class="why">· ${esc(srcs)} · click a tag to drop it</span></div>
      <div class="chips" style="margin-top:6px">${chips}</div></div></div>`;
  }
  const status = out ? ` <span class="badge b-${out.status}">${out.status}</span>` : '';
  const box = `<input type="checkbox" data-i="${i}" checked ${dis} aria-label="approve">`;
  if (c.action === 'add_to_album')
    return `<div class="row">${box}<div class="what">Add to album <b>${esc(c.new)}</b>${c.album_id ? '' : ' (new album)'}${status}<div class="why">${why}</div></div></div>`;
  if (c.action === 'set_title')
    return `<div class="row">${box}<div class="what">Title${status} <span class="why">was: ${esc(c.old || '(blank)')}</span>
      <input type="text" data-i="${i}" value="${esc(c.new)}" ${dis}></div></div>`;
  if (c.action === 'set_description')
    return `<div class="row">${box}<div class="what">Description${status}${c.old
      ? ` <span class="why">re-worded; replaces:</span><div class="why" style="margin:2px 0 4px;font-style:italic">${esc(c.old)}</div>` : ''}
      <textarea data-i="${i}" ${dis}>${esc(c.new)}</textarea></div></div>`;
  return `<div class="row"><div class="what">${esc(c.action)}: ${esc(c.new)}</div></div>`;
}

// Approve/Reject only hand the photo to the server's background worker, which makes
// the Flickr calls in order; the page moves straight on and polls for outcomes.
async function decide(approve) {
  if (!item || busy) return; busy = true;
  const payload = choices.map((ch, i) => item.changes[i].action === 'add_tags'
    ? {approve: approve && ch.tags.some(t => t.on), value: ch.tags.filter(t => t.on).map(t => t.t)}
    : {approve: approve && ch.approve, value: ch.value});
  try {
    await api('/api/item/' + item.photo_id + '/decide', {choices: payload});
    if (!approve) toast('Rejected — won\'t be proposed again');
    ids.splice(idx, 1); if (idx >= ids.length) idx = Math.max(0, ids.length - 1);
    pollJobs(); await show();
  } catch (e) { toast('Error: ' + e.message); }
  busy = false;
}

async function processNow() {
  if (!item || busy) return; busy = true;
  try { await api('/api/item/' + item.photo_id + '/process', {}); toast('Processing in the background…'); pollJobs(); await show(); }
  catch (e) { toast('Error: ' + e.message); }
  busy = false;
}
$('#goto').onsubmit = async e => {
  e.preventDefault(); const q = $('#gotoq').value.trim(); if (!q) return;
  try {
    const {photo_id} = await api('/api/find?q=' + encodeURIComponent(q));
    const at = ids.indexOf(photo_id);
    if (at >= 0) idx = at; else { ids.splice(idx, 0, photo_id); }
    $('#gotoq').value = ''; $('#gotoq').blur(); await show();
  } catch (err) { toast(err.message); }
};

let jobSeq = 0, failedSeen = 0;
async function pollJobs() {
  try {
    const j = await api('/api/jobs?since=' + jobSeq); jobSeq = j.seq;
    for (const f of j.finished) {
      if (f.status === 'failed') { failedSeen++; toast(`⚠ ${f.title}: ${f.error || f.failed + ' change(s) failed'}` + (f.kind === 'apply' ? ' — see Failed to retry' : '')); }
      else if (f.kind === 'process') toast(`Processed ${f.title}: ${f.status === 'pending' ? 'changes ready to review' : 'nothing to change'}`);
      if (f.kind === 'process' && item && f.photo_id === item.photo_id) show();   // refresh the open photo
    }
    const el = $('#jobs');
    const n = j.applying + j.processing;
    el.hidden = !n && !failedSeen;
    el.classList.toggle('bad', !!failedSeen && !n);
    el.innerHTML = n ? `<span class="spin">⟳</span> ` + [j.applying && `Applying ${j.applying}`, j.processing && `Processing ${j.processing}`].filter(Boolean).join(' · ')
                     : (failedSeen ? `${failedSeen} failed` : '');
    if (j.finished.length) loadSummary();
  } catch (e) { /* server busy or restarting; try again next tick */ }
}
setInterval(pollJobs, 1500);
function move(d) { if (!ids.length) return; idx = (idx + d + ids.length) % ids.length; show(); }

document.addEventListener('keydown', e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (e.key === 'Enter' && (!typing || e.metaKey || e.ctrlKey) && $('#ok')) { e.preventDefault(); decide(true); }
  else if (typing) return;
  else if (e.key === 'r' && $('#no')) decide(false);
  else if (e.key === 'p' && $('#proc')) processNow();
  else if (e.key === 'ArrowRight' || e.key === 's') move(1);
  else if (e.key === 'ArrowLeft') move(-1);
});
$('#status').onchange = () => { idx = 0; loadQueue(); };
$('#sort').onchange = () => { idx = 0; loadQueue(); };
loadSummary('pending').then(loadQueue).then(pollJobs).catch(e => { $('#main').innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`; });
