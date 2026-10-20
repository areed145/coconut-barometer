"""Local review tool for the per-photo queue: one photo at a time, approve each change,
and the Flickr calls are made right away.

    python -m flickr_organizer review      # opens http://127.0.0.1:8765

The server listens on 127.0.0.1 only and every API request must carry a random
per-run token, so other web pages can't drive it.
"""
from __future__ import annotations

import json
import queue
import re
import secrets
import threading
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .apply import apply_changes, outcomes
from .changes import META_ACTIONS, Change, split_tags
from .planner import photo_page

STATUSES = ("pending", "applying", "failed", "applied", "rejected", "none", "unprocessed")


class ReviewApp:
    """The review tool's logic. decide() and process() only queue a job; a single
    background worker makes the Flickr (and vision) calls in order, so the rate limit
    holds, and records the outcome. Photos left "applying" when the tool stopped resume.

    cfg/provider are needed only to process photos from the viewer; provider is a
    zero-argument factory so the vision SDK loads on first use."""

    def __init__(self, store, client, log=lambda *a: None, background: bool = True,
                 cfg: dict | None = None, provider=None, process_kw: dict | None = None):
        self.store, self.client, self.log = store, client, log
        self.background = background
        self.cfg, self._provider_factory, self._provider = cfg, provider, None
        self.process_kw = process_kw or {}
        self.jobs: queue.Queue[tuple[str, str]] = queue.Queue()   # ("apply" | "process", photo_id)
        self.processing: set[str] = set()
        self.recent: deque[dict] = deque(maxlen=20)   # finished jobs, newest last
        self._seq = 0
        self.licenses = self._licenses()
        if background:
            threading.Thread(target=self._worker, name="review-worker", daemon=True).start()
            for pid in reversed(store.queue_ids(["applying"])):   # oldest first
                self.jobs.put(("apply", pid))

    def summary(self) -> dict:
        counts = self.store.queue_counts()
        counts["unprocessed"] = len(self.store.unqueued_ids())
        return {s: counts.get(s, 0) for s in STATUSES}

    def queue(self, status: str = "pending", sort: str = "newest") -> list[str]:
        return self.store.unqueued_ids(sort) if status == "unprocessed" else self.store.queue_ids([status], sort)

    def _licenses(self) -> dict[str, str]:
        """License id -> name, from Flickr once, then cached in the database."""
        cached = self.store.get_meta("licenses")
        if cached:
            return json.loads(cached)
        try:
            names = {str(l["id"]): l["name"] for l in
                     self.client.call("flickr.photos.licenses.getInfo")["licenses"]["license"]}
        except Exception:
            return {}
        self.store.set_meta("licenses", json.dumps(names))
        return names

    def find(self, text: str) -> str | None:
        """A photo id, or a Flickr photo URL (…/photos/<user>/<id>…), if it's in the library."""
        m = re.search(r"/photos/[^/]+/(\d+)", text) or re.fullmatch(r"\s*(\d+)\s*", text)
        pid = m.group(1) if m else None
        return pid if pid and self.store.photo(pid) else None

    def item(self, photo_id: str) -> dict | None:
        q, p = self.store.queue_item(photo_id), self.store.photo(photo_id)
        if not p:
            return None
        if not q:
            q = {"status": "unprocessed", "changes": [], "result": None}
        if photo_id in self.processing:
            q = {**q, "status": "processing"}
        titles = {a["id"]: a["title"] for a in self.store.albums()}
        albums = sorted(titles.get(a, a) for a in self.store.memberships().get(photo_id, set()))
        large = (f"https://live.staticflickr.com/{p['server']}/{photo_id}_{p['secret']}_b.jpg"
                 if p["server"] and p["secret"] else p["thumb_url"])
        result = q["result"] if q["status"] != "applying" else None
        return {
            "photo_id": photo_id, "status": q["status"], "result": result,
            "changes": [Change.from_dict(c).to_dict() for c in q["changes"]],
            "photo": {"title": p["title"], "description": p["description"], "tags": (p["tags"] or "").split(),
                      "albums": albums, "date_taken": p["date_taken"], "image": large, "thumb": p["thumb_url"],
                      "url": photo_page(self.store.get_meta("nsid", "me"), photo_id),
                      "camera": self.store.cameras().get(photo_id, ""), "media": p["media"]},
            "stats": {"views": p["views"], "faves": p["faves"], "comments": p["comments"],
                      "license": self.licenses.get(str(p["license"]), p["license"]) if p["license"] is not None else None,
                      "width": p["width"], "height": p["height"], "uploaded": p["date_upload"],
                      **self.store.engagement(photo_id)},
        }

    def decide(self, photo_id: str, choices: list[dict]) -> dict:
        """choices: one per queued change, in order: {"approve": bool, "value": ...} where value
        is your edited title/description, or the list of tags you kept. Returns at once."""
        q = self.store.queue_item(photo_id)
        if not q:
            raise KeyError(photo_id)
        if q["status"] == "applying" or photo_id in self.processing:
            return {"status": q["status"]}
        self.store.queue_finish(photo_id, "applying", {"choices": choices})   # survives a restart
        if not self.background:
            return self._apply(self.store, photo_id)
        self.jobs.put(("apply", photo_id))
        return {"status": "applying"}

    def process(self, photo_id: str) -> dict:
        """(Re-)process one photo or video now: EXIF, classify, plan. Returns at once."""
        if not self.store.photo(photo_id):
            raise KeyError(photo_id)
        q = self.store.queue_item(photo_id)
        if (q and q["status"] == "applying") or photo_id in self.processing:
            return {"status": "busy"}
        if self.cfg is None:
            raise RuntimeError("processing isn't available in this session")
        self.processing.add(photo_id)
        if not self.background:
            return self._process(self.store, photo_id)
        self.jobs.put(("process", photo_id))
        return {"status": "processing"}

    def jobs_status(self, since: int = 0) -> dict:
        return {"applying": self.store.queue_counts().get("applying", 0), "processing": len(self.processing),
                "finished": [j for j in self.recent if j["seq"] > since], "seq": self._seq}

    def wait(self) -> None:
        """Block until every queued job is done (tests, shutdown)."""
        self.jobs.join()

    def _worker(self) -> None:
        # sqlite connections belong to one thread, so the worker opens its own.
        store = type(self.store)(self.store.path)
        while True:
            kind, pid = self.jobs.get()
            try:
                out = self._apply(store, pid) if kind == "apply" else self._process(store, pid)
            except Exception as e:  # network trouble etc.
                if kind == "apply":   # keep it under Failed to retry
                    store.queue_finish(pid, "failed", {"error": str(e)})
                self.processing.discard(pid)
                out = {"status": "failed", "error": str(e)}
            finally:
                self.jobs.task_done()
            photo = store.photo(pid)
            self._seq += 1
            self.recent.append({"seq": self._seq, "kind": kind, "photo_id": pid, "status": out["status"],
                                "title": (photo["title"] if photo else "") or pid,
                                "applied": out.get("stats", {}).get("applied", 0),
                                "failed": out.get("stats", {}).get("failed", 0), "error": out.get("error", "")})

    def _process(self, store, photo_id: str) -> dict:
        from .pipeline import process

        try:
            vcfg = self.cfg["vision"]
            if self._provider is None and self._provider_factory and vcfg.get("enabled", True) and vcfg["categories"]:
                self._provider = self._provider_factory()
            stats = process(self.cfg, store, self.client, self._provider, photo_ids=[photo_id], rebuild=True,
                            include_videos=True, log=lambda *a: None, **self.process_kw)
        finally:
            self.processing.discard(photo_id)
        if stats["retry_later"]:
            return {"status": "failed", "error": "EXIF or classification failed; try again"}
        return {"status": store.queue_item(photo_id)["status"]}   # pending | none

    def _apply(self, store, photo_id: str) -> dict:
        q = store.queue_item(photo_id)
        choices = (q["result"] or {}).get("choices", [])
        changes = [_with_choice(Change.from_dict(d), choice) for d, choice in zip(q["changes"], choices)]
        # (apply_changes re-reads the photo from Flickr before any title/description write.)
        stats = apply_changes(self.client, store, changes, log=self.log)
        result = {"stats": stats, "changes": outcomes(store, changes)}
        status = "failed" if stats["failed"] else ("applied" if stats["applied"] else "rejected")
        store.queue_finish(photo_id, status, result)
        return {"status": status, **result}


def _with_choice(c: Change, choice: dict) -> Change:
    """Your decision on one change from the page: {"approve": bool, "value": text or kept tags}.
    (Photos queued for applying before this format sent "target"; that still works.)"""
    c.approved = bool(choice.get("approve"))
    value = choice.get("value", choice.get("target"))
    if c.action == "add_tags":
        kept = split_tags(value if value is not None else c.new)
        c.edited = kept if kept != c.tags else None
        c.approved = c.approved and bool(kept)
    elif value is not None and c.action in META_ACTIONS and str(value).strip() != c.new:
        c.edited = str(value).strip()
    return c


def serve(app: ReviewApp, port: int = 8765, open_browser: bool = True) -> None:
    token = secrets.token_urlsafe(24)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, api: bool) -> bool:
            if self.headers.get("Host") not in hosts:          # blocks DNS rebinding
                self._send(403, {"error": "bad host"})
                return False
            if api and self.headers.get("X-Token") != token:   # blocks other pages
                self._send(403, {"error": "bad token"})
                return False
            return True

        def do_GET(self):
            url = urlparse(self.path)
            if not self._guard(api=url.path.startswith("/api/")):
                return
            if url.path == "/":
                return self._send(200, PAGE.replace("__TOKEN__", json.dumps(token)), "text/html")
            if url.path == "/api/summary":
                return self._send(200, app.summary())
            if url.path == "/api/queue":
                qs = parse_qs(url.query)
                return self._send(200, app.queue(qs.get("status", ["pending"])[0], qs.get("sort", ["newest"])[0]))
            if url.path == "/api/jobs":
                return self._send(200, app.jobs_status(int(parse_qs(url.query).get("since", ["0"])[0])))
            if url.path == "/api/find":
                pid = app.find(parse_qs(url.query).get("q", [""])[0])
                return self._send(200 if pid else 404, {"photo_id": pid} if pid else {"error": "not in your library"})
            if url.path.startswith("/api/item/"):
                item = app.item(url.path.rsplit("/", 1)[1])
                return self._send(200 if item else 404, item or {"error": "not found"})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            if not self._guard(api=True):
                return
            parts = url.path.strip("/").split("/")   # api/item/<id>/decide
            if len(parts) == 4 and parts[:2] == ["api", "item"] and parts[3] == "process":
                try:
                    return self._send(200, app.process(parts[2]))
                except KeyError:
                    return self._send(404, {"error": "not in your library"})
                except Exception as e:
                    return self._send(500, {"error": str(e)})
            if len(parts) == 4 and parts[:2] == ["api", "item"] and parts[3] == "decide":
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                try:
                    return self._send(200, app.decide(parts[2], body.get("choices", [])))
                except KeyError:
                    return self._send(404, {"error": "not in queue"})
                except Exception as e:  # surface Flickr/network errors in the page
                    return self._send(500, {"error": str(e)})
            self._send(404, {"error": "not found"})

    server = HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Review tool running at {url}  (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        left = app.jobs.qsize()
        print(f"\nStopped.{f' {left} photo(s) still to apply; they resume next time you run review.' if left else ''}")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Photo Review Queue</title>
<style>
:root{--bg:#f6f5f3;--fg:#1c1917;--muted:#78716c;--card:#fff;--line:#e7e5e4;--accent:#0063dc;--ok:#15803d;--no:#dc2626;--warn:#b45309;--chip:#eef4fd}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#171513;--fg:#f5f5f4;--muted:#a8a29e;--card:#24211f;--line:#3f3a36;--accent:#5aa2ff;--ok:#4ade80;--no:#f87171;--warn:#fbbf24;--chip:#1e2a3a}}
:root[data-theme="dark"]{--bg:#171513;--fg:#f5f5f4;--muted:#a8a29e;--card:#24211f;--line:#3f3a36;--accent:#5aa2ff;--ok:#4ade80;--no:#f87171;--warn:#fbbf24;--chip:#1e2a3a}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0 8px 0 0}.muted{color:var(--muted)}.sp{flex:1}
select,button,input,textarea{font:inherit;color:var(--fg)}
select,button{padding:6px 12px;border-radius:7px;border:1px solid var(--line);background:var(--card);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button.danger{color:var(--no)}button:disabled{opacity:.5;cursor:default}
kbd{font:11px ui-monospace,monospace;border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:0 4px;color:var(--muted)}
main{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(320px,1fr);gap:16px;padding:16px;max-width:1500px;margin:auto}
@media (max-width:860px){main{grid-template-columns:1fr}}
.pic{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;position:sticky;top:64px;align-self:start}
.pic img{display:block;width:100%;max-height:72vh;object-fit:contain;background:#000}
.pic .cap{padding:8px 12px;display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.pic a{color:var(--accent)}
.panel{display:flex;flex-direction:column;gap:12px;min-width:0}
.box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.box h2{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:0 0 8px}
.cur dt{font-size:12px;color:var(--muted)}.cur dd{margin:0 0 6px;overflow-wrap:anywhere}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{padding:2px 9px;border-radius:999px;font-size:12px;border:1px solid var(--line);user-select:none}
.chip.prop{border-color:var(--accent);background:var(--chip);color:var(--accent);cursor:pointer}
.chip.prop.off{background:none;border-color:var(--line);color:var(--muted);text-decoration:line-through}
.chip small{opacity:.65;margin-left:4px}
.chip.applied{border-color:var(--ok);color:var(--ok)}.chip.rejected{color:var(--muted);text-decoration:line-through}.chip.failed{border-color:var(--no);color:var(--no)}
.row{display:flex;gap:8px;align-items:flex-start;padding:6px 0;border-top:1px solid var(--line)}.row:first-of-type{border-top:0}
.row input[type=checkbox]{margin-top:3px;width:16px;height:16px;accent-color:var(--accent)}
.row .what{flex:1;min-width:0}.row .why{font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.row input[type=text],.row textarea{width:100%;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg)}
.row textarea{resize:vertical;min-height:60px}
.badge{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid currentColor}
.b-applied{color:var(--ok)}.b-rejected{color:var(--muted)}.b-failed{color:var(--no)}.b-pending{color:var(--accent)}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:8px 14px;border-radius:8px;font-size:13px;opacity:0;transition:opacity .2s;pointer-events:none;max-width:90vw}
.toast.show{opacity:1}
.jobs{font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid var(--accent);color:var(--accent)}
.jobs.bad{border-color:var(--no);color:var(--no)}
.spin{display:inline-block;animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:8px}
.tile{background:var(--bg);border-radius:8px;padding:8px 10px}.tile b{display:block;font-size:18px;font-variant-numeric:tabular-nums}
.tile span{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.meta{font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;gap:4px 12px;margin-bottom:6px}
.up{color:var(--ok)}a.chip{color:inherit;text-decoration:none}a.chip:hover{border-color:var(--accent)}
.cmt{border-top:1px solid var(--line);padding:6px 0;font-size:13px}.cmt:first-child{border-top:0}
.cmt .who{font-size:12px;color:var(--muted)}.cmt .txt{overflow-wrap:anywhere;max-height:6.5em;overflow:hidden}
.empty{padding:60px 16px;text-align:center;color:var(--muted);grid-column:1/-1}
</style></head><body>
<header>
  <h1>Review queue</h1>
  <select id="status" aria-label="Show"></select>
  <select id="sort" aria-label="Sort">
    <option value="newest">Newest first</option><option value="oldest">Oldest first</option>
    <option value="views">Most viewed</option><option value="faves">Most faved</option><option value="comments">Most commented</option>
  </select>
  <span class="muted" id="pos"></span>
  <form id="goto" style="display:flex;gap:6px"><input id="gotoq" placeholder="Go to photo ID or Flickr URL" aria-label="Go to photo"
    style="padding:5px 9px;border:1px solid var(--line);border-radius:7px;background:var(--card);width:240px"></form>
  <span class="sp"></span>
  <span id="jobs" class="jobs" hidden></span>
  <span class="muted" style="font-size:12px"><kbd>Enter</kbd> approve · <kbd>R</kbd> reject · <kbd>P</kbd> process · <kbd>→</kbd> skip · <kbd>←</kbd> back</span>
</header>
<main id="main"><div class="empty">Loading…</div></main>
<div class="toast" id="toast"></div>
<script>
const TOKEN = __TOKEN__;
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
</script></body></html>"""
