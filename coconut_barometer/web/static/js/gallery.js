// Photos (one page for every filtered view: album, tag, year, camera, search), albums, and the lightbox.
import { html, useState, useEffect, useLayoutEffect, useRef, useMemo, useCallback, api, num, day, plain, pick, fallback, photosLink, toast, signedIn, share, today, dayLabel } from "./lib.js";

const THUMB = ["n", "w", "m", "q"];
import { Chart } from "./chart.js";
import { MiniMap } from "./leaflet.js";
import { aspect, justify } from "./justify.js";

const PAGE = 60;

// Loads pages from fetchPage(offset) as the sentinels scroll into view: down with more(), from the top or from wherever
// jump(offset) went (the timeline, #126), and back up to the top with less(). photos are those from `start` on.
// less(before) calls before() just as the photos above go in, so the grid can keep what's on screen where it is.
function usePaged(fetchPage, deps) {
  const [view, setView] = useState({ photos: [], start: 0, total: null });
  const [loading, setLoading] = useState(false);
  const state = useRef({ photos: [], start: 0, total: null, loading: false, gen: 0 });
  const load = useCallback(async (up, before) => {
    const s = state.current;
    if (s.loading || (up ? s.start === 0 : s.total !== null && s.start + s.photos.length >= s.total)) return;
    s.loading = true; setLoading(true);
    const gen = s.gen, from = up ? Math.max(0, s.start - PAGE) : s.start + s.photos.length;
    try {
      const page = await fetchPage(from);
      if (gen !== state.current.gen) return;           // filters changed, or it jumped, meanwhile
      if (up) { before?.(); s.photos = [...page.photos.slice(0, s.start - from), ...s.photos]; s.start = from; }
      else s.photos = [...s.photos, ...page.photos];
      s.total = page.total;
      setView({ photos: s.photos, start: s.start, total: s.total });
    } finally { if (gen === state.current.gen) { s.loading = false; setLoading(false); } }
  }, deps);
  const jump = (start) => {
    state.current = { photos: [], start, total: state.current.total, loading: false, gen: state.current.gen + 1 };
    setView({ photos: [], start, total: state.current.total });
    load(false);
  };
  useEffect(() => {
    state.current = { photos: [], start: 0, total: null, loading: false, gen: state.current.gen + 1 };
    setView({ photos: [], start: 0, total: null });
    load(false);
  }, deps);
  return { ...view, loading, more: useCallback(() => load(false), [load]), less: useCallback((before) => load(true, before), [load]), jump };
}

// A tile in the row at the top of the screen, under the sticky header, in the grid `el`: the last in the row, the
// one furthest along the list (a row can start in one month and end in the next, where a jump to that month lands).
// The first when the row doesn't reach the edge; a point between rows misses, so try lower.
function tileAt(el) {
  if (!el) return null;
  const r = el.getBoundingClientRect(), y = Math.max(r.top, document.querySelector("header.top")?.getBoundingClientRect().bottom || 0);
  for (const dy of [8, 16, 24]) for (const x of [r.right - 4, r.left + 4]) {
    const t = document.elementFromPoint(x, Math.min(y + dy, innerHeight - 1))?.closest(".tile");
    if (t && el.contains(t)) return t;
  }
  return null;
}

export function PhotoGrid({ paged, slideshow = 0, onAt }) {
  const { photos, start, total, loading, more, less } = paged;
  const [open, setOpen] = useState(null);           // index into photos
  const [autoplay, setAutoplay] = useState(false);
  useEffect(() => { if (slideshow && photos.length) { setAutoplay(true); setOpen(0); } }, [slideshow]);   // a click on Slideshow
  const sentinel = useRef(null);
  const top = useRef(null);
  const box = useRef(null);
  const [width, setWidth] = useState(0);
  // The grid's width, at most once a frame while resizing.
  useEffect(() => {
    let frame = 0;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => box.current && setWidth(Math.floor(box.current.clientWidth)));
    });
    if (box.current) ro.observe(box.current);
    return () => { ro.disconnect(); cancelAnimationFrame(frame); };
  }, [total === 0]);
  // Rows depend only on the photos before them, so a new page only adds rows (and refills the last one).
  const narrow = width && width < 520;
  const gap = narrow ? 3 : 6;
  const rows = useMemo(() => (width ? justify(photos.map(aspect), width, narrow ? 120 : 220, gap) : []), [photos, width]);
  // An observer only reports changes: if it fired while a page was loading (ignored) and the sentinel is still
  // within the margin once the page lands, nothing fires again and scrolling stalls. So re-arm after every page
  // (a fresh observer always reports the current state), and back it with a plain scroll/resize check.
  useEffect(() => {
    const el = sentinel.current;
    if (!el) return;
    const near = () => el.getBoundingClientRect().top < innerHeight + 800 && more();
    const io = new IntersectionObserver((e) => e.some((x) => x.isIntersecting || x.intersectionRatio > 0) && more(), { rootMargin: "800px" });
    io.observe(el);
    addEventListener("scroll", near, { passive: true });
    addEventListener("resize", near);
    return () => { io.disconnect(); removeEventListener("scroll", near); removeEventListener("resize", near); };
  }, [more, photos.length, total]);
  // Above, after a jump: the same, upward. When the photos above go in, the one at the top of the screen stays put.
  const anchor = useRef(null);
  const before = () => {
    const t = tileAt(box.current);
    anchor.current = t && { id: t.dataset.id, top: t.getBoundingClientRect().top };
  };
  useLayoutEffect(() => {
    const a = anchor.current, t = a && box.current?.querySelector(`[data-id="${a.id}"]`);
    anchor.current = null;
    if (t) scrollBy(0, t.getBoundingClientRect().top - a.top);
  }, [start]);
  useEffect(() => {
    const el = top.current;
    if (!el || !start) return;
    const io = new IntersectionObserver((e) => e.some((x) => x.isIntersecting) && less(before), { rootMargin: "800px" });
    io.observe(el);
    return () => io.disconnect();
  }, [less, start, rows.length]);
  // Where the screen is in the list (for the timeline): the photo at the top of it.
  useEffect(() => {
    if (!onAt) return;
    let frame = 0;
    const at = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => { const t = tileAt(box.current); t && onAt(+t.dataset.i); }); };
    at();
    addEventListener("scroll", at, { passive: true });
    return () => { removeEventListener("scroll", at); cancelAnimationFrame(frame); };
  }, [onAt, rows]);
  if (total === 0) return html`<div class="empty">No photos here.</div>`;
  return html`
    ${start > 0 && html`<div class="sentinel" ref=${top}></div>`}
    <div class="justified" ref=${box}>
      ${rows.map((r) => html`<div class=${"jrow" + (r.last ? " last" : "")} key=${photos[r.start].id}>
        ${photos.slice(r.start, r.end).map((p, j) => {
          const w = aspect(p) * r.height;
          return html`<button class="tile" key=${p.id} data-id=${p.id} data-i=${start + r.start + j} onClick=${() => setOpen(r.start + j)}
              aria-label=${p.title || "Untitled photo"}
              style=${{ width: `${w}px`, height: `${r.height}px` }}>
            <img loading="lazy" decoding="async" alt="" src=${pick(p.sizes, THUMB)} onError=${fallback(p.sizes, THUMB)}
                 srcset=${[p.sizes.n && p.sizes.n + " 320w", p.sizes.w && p.sizes.w + " 400w", p.sizes.z && p.sizes.z + " 640w",
                           p.sizes.c && p.sizes.c + " 800w"].filter(Boolean).join(", ")}
                 sizes=${`${Math.ceil(w)}px`} />
            ${p.media === "video" && html`<span class="vid">video</span>`}
            <span class="cap">${p.title || "Untitled"}<br />${num(p.views)} views · ${num(p.faves)} faves</span>
          </button>`;
        })}
      </div>`)}
    </div>
    <div class="sentinel" ref=${sentinel}></div>
    <div class="more">${loading ? "Loading…" : total !== null && (start ? `${num(start + 1)}–${num(start + photos.length)} of ${num(total)}`
      : `${num(photos.length)} of ${num(total)}`)}</div>
    ${open !== null && html`<${Lightbox} photos=${photos} index=${open} onIndex=${setOpen} autoplay=${autoplay}
        onClose=${() => { setOpen(null); setAutoplay(false); }} onNearEnd=${more} />`}`;
}

const SORTS = [["newest", "Newest uploads"], ["oldest", "Oldest uploads"], ["taken", "Date taken"], ["views", "Most viewed"],
               ["faves", "Most faved"], ["comments", "Most commented"], ["title", "Title"]];
const FILTERS = ["album", "tag", "year", "on", "day", "near", "camera", "lens", "setting", "media", "q", "sort", "is"];

// How a `setting` filter from the Gear page reads: "focal:24-28" is ["Focal length", "24 mm to 28 mm"].
const shutter = (s) => (s >= 1 ? `${+s.toFixed(1)} s` : `1/${Math.round(1 / s)}`);
function setting(v) {
  const [name, span = ""] = v.split(":"), [lo, hi] = span.split("-").map((x) => (x === "" ? null : +x));
  const fmt = { focal: (x) => `${x} mm`, f: (x) => `f/${x}`, iso: String, shutter }[name] || String;
  return [{ focal: "Focal length", f: "Aperture", iso: "ISO", shutter: "Shutter" }[name] || "Setting",
          hi == null ? `${fmt(lo)} and over` : !lo ? `under ${fmt(hi)}` : `${fmt(lo)} to ${fmt(hi)}`];
}

// The Photos page, and every filtered view of it: the filters live in the address
// (#/photos/photostream?album=…&tag=…&year=…&on=…&camera=…&media=…&q=…&sort=…), so any of them can be linked to.
export function Photos({ params = {} }) {
  const f = Object.fromEntries(FILTERS.map((k) => [k, params[k] || ""]));
  const set = (patch) => location.replace(photosLink({ ...f, ...patch }));     // no history entry per keystroke
  const [q, setQ] = useState(f.q);
  const [album, setAlbum] = useState(null);
  const [years, setYears] = useState([]);
  const [presets, setPresets] = useState([]);
  const [showPresets, setShowPresets] = useState(!!f.is);
  const [slideshow, setSlideshow] = useState(0);
  const active = f.is ? f.is.split(",").filter(Boolean) : [];
  const togglePreset = (id) => set({ is: (active.includes(id) ? active.filter((x) => x !== id) : [...active, id]).join(",") });
  useEffect(() => setQ(f.q), [f.q]);
  useEffect(() => { const t = setTimeout(() => q.trim() !== f.q && set({ q: q.trim() }), 350); return () => clearTimeout(t); }, [q]);
  useEffect(() => {
    api("/api/photo-years").then(setYears).catch(() => {});
    api("/api/photo-presets").then(setPresets).catch(() => {});
  }, []);
  useEffect(() => {
    setAlbum(null);
    if (f.album) api(`/api/albums/${encodeURIComponent(f.album)}`, { params: { limit: 1 } }).then(setAlbum).catch(() => set({ album: "" }));
  }, [f.album]);
  const key = FILTERS.map((k) => f[k]).join("|");
  const query = { album: f.album, tag: f.tag, year: f.year, on: f.on, camera: f.camera, lens: f.lens, setting: f.setting, day: f.day,
                  near: f.near, media: f.media, q: f.q, sort: f.sort || "newest", is: f.is };
  const paged = usePaged((offset) => api("/api/photos", { params: { ...query, offset, limit: PAGE } }), [key]);
  // The timeline (#126), for the date sorts: photos per month, and the photo at the top of the screen (told to the
  // timeline alone, so scrolling doesn't redraw the page).
  const [months, setMonths] = useState([]);
  useEffect(() => { setMonths([]); if (DATED.includes(query.sort)) api("/api/photo-months", { params: query }).then(setMonths).catch(() => {}); }, [key]);
  const at = useMemo(() => ({ tell: null }), []);
  const wrap = useRef(null);
  const landing = useRef(null);                     // the photo a jump goes to, until its page is in
  const toTile = (t) => scrollBy(0, t.getBoundingClientRect().top - (document.querySelector("header.top")?.getBoundingClientRect().bottom || 0) - 8);
  const jump = (offset) => {
    const loaded = wrap.current?.querySelector(`.tile[data-i="${offset}"]`);
    if (loaded) return toTile(loaded);
    landing.current = offset;
    paged.jump(offset);
  };
  useLayoutEffect(() => {                           // only once the page is laid out: while it's empty the page is shorter
    const t = landing.current !== null && wrap.current?.querySelector(`.tile[data-i="${landing.current}"]`);
    if (t) { landing.current = null; toTile(t); }
  });
  const timeline = DATED.includes(query.sort) && paged.total > 2 * PAGE && months.length > 1;
  const chips = [["album", "Album", album?.title || "…"], ["tag", "Tag", f.tag], ["year", "Taken in", f.year],
                 ["on", "On this day", f.on && dayLabel(f.on)], ["day", "Taken on", f.day],
                 ["near", "Within 500 m of", f.near.replace(",", ", ")], ["camera", "Camera", f.camera], ["lens", "Lens", f.lens],
                 ["setting", ...(f.setting ? setting(f.setting) : [])]]
    .filter(([k]) => f[k]);
  const label = Object.fromEntries(presets.map((p) => [p.id, p.label]));
  const groups = [...new Set(presets.map((p) => p.group))];
  return html`
    ${f.album && html`<a href="#/photos/albums">← Albums</a>
      <h1 class="title">${album ? album.title : "…"}
        ${album && html` <button class="link share" onClick=${() => share(`/a/${album.id}`, album.title)}>Share</button>`}</h1>
      ${album?.description && html`<p class="muted">${plain(album.description)}</p>`}`}
    ${f.tag && !f.album && html`<h1 class="title">#${f.tag}</h1>`}
    <div class="toolbar">
      <input type="search" placeholder="Search titles, descriptions, tags" value=${q}
             onInput=${(e) => setQ(e.target.value)} aria-label="Search" />
      <select value=${f.sort || "newest"} onChange=${(e) => set({ sort: e.target.value === "newest" ? "" : e.target.value })} aria-label="Sort">
        ${SORTS.map(([k, label]) => html`<option value=${k}>${label}</option>`)}
      </select>
      <select value=${f.media} onChange=${(e) => set({ media: e.target.value })} aria-label="Media">
        <option value="">Photos and videos</option><option value="photo">Photos</option><option value="video">Videos</option>
      </select>
      <select value=${f.year} onChange=${(e) => set({ year: e.target.value })} aria-label="Year taken">
        <option value="">Any year</option>${years.map((y) => html`<option value=${y.year}>${y.year} (${num(y.count)})</option>`)}
      </select>
      <button class=${f.on ? "on" : ""} aria-pressed=${!!f.on} onClick=${() => set({ on: f.on ? "" : today(), sort: f.on ? f.sort : "taken" })}
              title="Photos taken on today's date, any year">On this day</button>
      <button class=${showPresets || active.length ? "on" : ""} onClick=${() => setShowPresets(!showPresets)}
              aria-expanded=${showPresets}>Presets${active.length ? ` (${active.length})` : ""}</button>
      <button onClick=${() => setSlideshow((n) => n + 1)} disabled=${!paged.total} title="Play these photos as a slideshow">▶ Slideshow</button>
      <span class="muted">${paged.total !== null && `${num(paged.total)} items`}</span>
    </div>
    ${showPresets && presets.length > 0 && html`<div class="presets">${groups.map((g) => html`<div class="pgroup" key=${g}>
        <span class="why">${g}</span>
        ${presets.filter((p) => p.group === g).map((p) => html`<button key=${p.id} class=${"preset" + (active.includes(p.id) ? " on" : "")}
            disabled=${!p.count && !active.includes(p.id)} aria-pressed=${active.includes(p.id)} onClick=${() => togglePreset(p.id)}
            title=${p.count ? `${num(p.count)} photos in the whole library` : "No photos match"}>${p.label} <span class="n">${num(p.count)}</span></button>`)}
      </div>`)}<p class="muted small">Presets combine with each other and with the filters above: a photo has to match all of them.</p></div>`}
    ${(chips.length > 0 || active.length > 0) && html`<div class="chips filters">${chips.map(([k, label, v]) => html`<span class="chip on" key=${k}>
        ${label}: <b>${v}</b> <button class="link" onClick=${() => set({ [k]: "" })} aria-label=${`Remove the ${label} filter`}>✕</button></span>`)}
      ${active.map((id) => html`<span class="chip on" key=${"is" + id}>${label[id] || id}
        <button class="link" onClick=${() => togglePreset(id)} aria-label=${`Remove ${label[id] || id}`}>✕</button></span>`)}
      ${chips.length + active.length > 1 && html`<button class="link" onClick=${() => location.replace(photosLink({ sort: f.sort }))}>Clear all</button>`}</div>`}
    <div ref=${wrap} class=${timeline ? "with-timeline" : ""}>
      <${PhotoGrid} paged=${paged} slideshow=${slideshow} onAt=${timeline ? (i) => at.tell?.(i) : null} />
    </div>
    ${timeline && html`<${Timeline} months=${months} at=${at} onJump=${jump} />`}`;
}

const DATED = ["newest", "oldest", "taken"];
const monthLabel = (m) => (m ? new Date(`${m}-15T12:00:00`).toLocaleDateString(undefined, { month: "short", year: "numeric" }) : "No date");

// The timeline (#126): a rail down the right edge. Every photo has the same share of its height, so a busy month is a
// longer stretch; the years are marked. Click or drag to jump to a month (arrow keys too); while scrolling, the month
// in view shows beside it. On phones it only appears while scrolling.
function Timeline({ months, at, onJump }) {
  const rail = useRef(null);
  const [h, setH] = useState(0);
  const [cur, setCur] = useState(0);                // the photo at the top of the screen
  const [drag, setDrag] = useState(null);           // the photo under the pointer, while dragging
  const [active, setActive] = useState(false);      // scrolled just now
  const marks = useMemo(() => { let o = 0; return months.map((m) => { const r = { ...m, at: o }; o += m.count; return r; }); }, [months]);
  const total = marks.length ? marks.at(-1).at + marks.at(-1).count : 0;
  const monthOf = (o) => {                          // the last month starting at or before photo o
    let lo = 0, hi = marks.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (marks[mid].at <= o) lo = mid; else hi = mid - 1; }
    return lo;
  };
  useEffect(() => { at.tell = setCur; return () => { at.tell = null; }; }, [at]);
  useEffect(() => { setActive(true); const t = setTimeout(() => setActive(false), 1500); return () => clearTimeout(t); }, [cur]);
  useEffect(() => {
    const ro = new ResizeObserver(() => setH(rail.current?.clientHeight || 0));
    if (rail.current) ro.observe(rail.current);
    return () => ro.disconnect();
  }, []);
  const y = (o) => (o / Math.max(1, total)) * h;
  const under = (e) => { const r = rail.current.getBoundingClientRect(); return Math.min(total - 1, Math.max(0, Math.floor((e.clientY - r.top) / r.height * total))); };
  const years = [];
  let seen = null;
  for (const m of marks) {                          // each year marked where its stretch starts, unless that crowds the last mark
    const yr = m.month?.slice(0, 4);
    if (!yr || yr === seen) continue;
    seen = yr;
    if (!years.length || y(m.at) - years.at(-1).y >= 18) years.push({ yr, y: y(m.at) });
  }
  const shown = drag ?? cur, i = monthOf(shown);
  const key = (e) => {
    const to = e.key === "ArrowDown" || e.key === "PageDown" ? i + 1 : e.key === "ArrowUp" || e.key === "PageUp" ? i - 1 : null;
    if (to === null || !marks[to]) return;
    e.preventDefault(); onJump(marks[to].at);
  };
  return html`<div class=${"timeline" + (active || drag !== null ? " active" : "")} ref=${rail} role="slider" tabindex="0"
      aria-label="Timeline" aria-valuemin="0" aria-valuemax=${total - 1} aria-valuenow=${shown} aria-valuetext=${monthLabel(marks[i]?.month)}
      onKeyDown=${key}
      onPointerDown=${(e) => { e.currentTarget.setPointerCapture(e.pointerId); setDrag(under(e)); }}
      onPointerMove=${(e) => drag !== null && setDrag(under(e))}
      onPointerUp=${() => { if (drag !== null) onJump(marks[monthOf(drag)].at); setDrag(null); }}
      onPointerCancel=${() => setDrag(null)}>
    ${years.map((m) => html`<span class="yr" key=${m.yr} style=${{ top: `${m.y}px` }}>${m.yr}</span>`)}
    <span class="thumb" style=${{ top: `${y(shown)}px` }}></span>
    <span class="bubble" style=${{ top: `${y(shown)}px` }}>${monthLabel(marks[i]?.month)}</span>
  </div>`;
}

export function Albums() {
  const [albums, setAlbums] = useState(null);
  const [q, setQ] = useState("");
  useEffect(() => { api("/api/albums").then(setAlbums); }, []);
  if (!albums) return html`<div class="boot">Loading…</div>`;
  const shown = albums.filter((a) => a.title.toLowerCase().includes(q.toLowerCase()));
  return html`
    <div class="toolbar">
      <input type="search" placeholder="Filter albums" value=${q} onInput=${(e) => setQ(e.target.value)} aria-label="Filter albums" />
      <span class="muted">${num(shown.length)} albums</span>
    </div>
    <div class="albums">
      ${shown.map((a) => html`<a class="album" key=${a.id} href=${photosLink({ album: a.id })}>
        <div class="cover">${a.cover_url && html`<img loading="lazy" alt="" src=${a.cover_url} />`}</div>
        <div class="meta"><b>${a.title}</b><span>${num(a.count)} items${a.date_update ? " · updated " + day(a.date_update) : ""}</span></div>
      </a>`)}
    </div>`;
}

// One photo on its own: where a share link (/p/<id>) lands. Closing it goes to the photostream.
export function SinglePhoto({ id }) {
  const [p, setP] = useState(undefined);         // undefined = loading, null = not found (or private)
  useEffect(() => { setP(undefined); api(`/api/photos/${encodeURIComponent(id)}`).then(setP).catch(() => setP(null)); }, [id]);
  if (p === undefined) return html`<div class="boot">Loading…</div>`;
  if (p === null) return html`<div class="empty">This photo isn't here. <a href=${photosLink()}>See the photostream</a></div>`;
  return html`<${Lightbox} photos=${[p]} index=${0} onIndex=${() => {}} onClose=${() => location.replace(photosLink())} />`;
}

// Photo detail, kept for the last few photos opened or preloaded, so stepping back and forth doesn't fetch it again.
const details = new Map();
function photoDetail(id) {
  const key = `${signedIn()}:${id}`;       // signed in sees more (private photos, locations)
  if (!details.has(key)) {
    details.set(key, api(`/api/photos/${id}`).catch((e) => { details.delete(key); throw e; }));
    if (details.size > 40) details.delete(details.keys().next().value);
  }
  return details.get(key);
}
// On a metered or slow connection, don't fetch what may never be looked at.
const frugal = () => { const c = navigator.connection; return !!c && (c.saveData || /(^|-)2g$/.test(c.effectiveType || "")); };
// The lightbox's image sizes, best first: as shown, and zoomed in.
const SHOWN = ["b", "l", "c", "z", "w", "n"], ZOOMED = ["k", "h", "l", "b", "c", "z", "w", "n"];

// Touch on the lightbox's photo (#129): swipe left or right for the next or previous photo and down to close; pinch or
// double-tap to zoom, and drag to pan while zoomed. Returns the photo's transform. The mouse keeps click-to-zoom.
const SWIPE = 60, CLOSE = 90, TAP = 10, DOUBLE = 300, MAX = 6;
function useTouch({ onPrev, onNext, onClose, reset, off }) {
  const [tf, setTf] = useState({ s: 1, x: 0, y: 0, live: false });
  const g = useRef({ pts: new Map(), tf: { s: 1, x: 0, y: 0 }, tap: 0, touch: false });
  const set = (t, live = false) => { g.current.tf = t; setTf({ ...t, live }); };
  useEffect(() => set({ s: 1, x: 0, y: 0 }), [reset]);
  const begin = (el) => {          // a gesture starts again whenever a finger is added or lifted
    const s = g.current, r = el.getBoundingClientRect();
    s.start = { pts: [...s.pts.values()].map((q) => ({ ...q })), tf: s.tf, c: { x: r.left + r.width / 2, y: r.top + r.height / 2 } };
  };
  const onPointerDown = (e) => {
    g.current.touch = e.pointerType !== "mouse";
    if (!g.current.touch || off || e.target.closest("button")) return;   // off: the mouse's zoom, which scrolls
    e.currentTarget.setPointerCapture(e.pointerId);
    g.current.pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    g.current.moved = g.current.pts.size > 1;
    begin(e.currentTarget);
  };
  const onPointerMove = (e) => {
    const s = g.current;
    if (!s.pts.has(e.pointerId)) return;
    s.pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const [a, b] = s.pts.values(), [a0, b0] = s.start.pts, t0 = s.start.tf, c = s.start.c;
    if (Math.hypot(a.x - a0.x, a.y - a0.y) > TAP) s.moved = true;
    if (b && b0) {                 // pinch: scale about the point between the fingers, which follows them
      const sc = Math.min(MAX, Math.max(1, t0.s * Math.hypot(b.x - a.x, b.y - a.y) / Math.max(1, Math.hypot(b0.x - a0.x, b0.y - a0.y))));
      const m = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, m0 = { x: (a0.x + b0.x) / 2, y: (a0.y + b0.y) / 2 };
      set({ s: sc, x: m.x - c.x - (m0.x - c.x - t0.x) * sc / t0.s, y: m.y - c.y - (m0.y - c.y - t0.y) * sc / t0.s }, true);
    } else if (t0.s > 1) set({ s: t0.s, x: t0.x + a.x - a0.x, y: t0.y + a.y - a0.y }, true);   // pan
    else {                         // swipe: follow the finger along whichever way it's going
      const dx = a.x - a0.x, dy = a.y - a0.y;
      set(Math.abs(dx) > Math.abs(dy) ? { s: 1, x: dx, y: 0 } : { s: 1, x: 0, y: Math.max(0, dy) }, true);
    }
  };
  const onPointerUp = (e) => {
    const s = g.current;
    if (!s.pts.delete(e.pointerId)) return;
    if (s.pts.size) return begin(e.currentTarget);
    const t = s.tf;
    if (!s.moved) {                // a tap: two in quick succession zoom in on that point, or back out
      const now = Date.now();
      if (now - s.tap < DOUBLE) {
        s.tap = 0;
        const c = s.start.c, k = 2.5;
        set(t.s > 1 ? { s: 1, x: 0, y: 0 } : { s: k, x: (e.clientX - c.x) * (1 - k), y: (e.clientY - c.y) * (1 - k) });
      } else s.tap = now;
      return;
    }
    if (t.s > 1.02) return set(t);                            // stays zoomed where it was left
    if (t.x <= -SWIPE && onNext) onNext();
    else if (t.x >= SWIPE && onPrev) onPrev();
    else if (t.y >= CLOSE) return onClose();
    set({ s: 1, x: 0, y: 0 });                                // too short: back into place
  };
  return { tf, touched: () => g.current.touch, handlers: { onPointerDown, onPointerMove, onPointerUp, onPointerCancel: onPointerUp } };
}

// Slideshow (#130): how long each photo stays, remembered on this device.
const PACES = [3, 5, 10];
const PACE_KEY = "cb-slideshow-secs";
const savedPace = () => { try { return PACES.find((n) => n === +localStorage.getItem(PACE_KEY)) || 5; } catch { return 5; } };

// The lightbox. A photo picked from a related strip (#124) is browsed with the rest of that strip, until "Back".
export function Lightbox(props) {
  const [alt, setAlt] = useState(null);             // { photos, index, label }
  const view = alt ? { ...props, photos: alt.photos, index: alt.index, onIndex: (i) => setAlt({ ...alt, index: i }), onNearEnd: null } : props;
  return html`<${LightboxView} ...${view} browsing=${alt?.label} onBack=${() => setAlt(null)}
    onRelated=${(photos, index, label) => setAlt({ photos, index, label })} />`;
}

// Photos taken nearby and on the same day, as two strips of thumbnails (#124).
function Related({ id, onPick, onClose }) {
  const [r, setR] = useState(null);
  useEffect(() => { setR(null); api(`/api/photos/${id}/related`).then(setR).catch(() => {}); }, [id]);
  if (!r) return null;
  const strip = (label, list, link) => list.length > 0 && html`<div class="related">
    <h3>${label} <a href=${link} onClick=${onClose}>See all</a></h3>
    <div class="strip">${list.map((q, i) => html`<button key=${q.id} onClick=${() => onPick(list, i, label)} aria-label=${q.title || "Untitled photo"}
        title=${q.title || "Untitled"}><img loading="lazy" alt="" src=${pick(q.sizes, ["q", "n", "m"])} onError=${fallback(q.sizes, ["q", "n", "m"])} /></button>`)}</div>
  </div>`;
  return html`${strip("Nearby", r.nearby, photosLink({ near: r.near?.join(","), sort: "taken" }))}
    ${strip("Same day", r.same_day, photosLink({ day: r.day, sort: "oldest" }))}`;
}

function LightboxView({ photos, index, onIndex, onClose, onNearEnd, autoplay = false, browsing, onBack, onRelated }) {
  const p = photos[index];
  const root = useRef(null);
  const [playing, setPlaying] = useState(false);
  const [held, setHeld] = useState(false);          // paused for now: the pointer is on the controls, or the tab is hidden
  const [secs, setSecs] = useState(savedPace);
  const [idle, setIdle] = useState(false);          // controls hidden while playing, until the pointer moves
  const full = useRef(false);                       // whether the slideshow went full screen (and so leaves it)
  const was = useRef(index);              // the index before, for the direction of travel
  const [detail, setDetail] = useState(null);
  const [timeline, setTimeline] = useState(null);
  const [zoom, setZoom] = useState(false);
  const [info, setInfo] = useState(true);
  const [sending, setSending] = useState(false);
  const sendToReview = async () => {
    if (sending) return; setSending(true);
    try {
      const r = await api(`/api/review/items/${p.id}/send`, { method: "POST" });
      toast(r.status === "processing" ? "Sent to review; processing it now" : r.status === "new" ? "Sent to review"
        : "Already in review");
      onClose();
      location.hash = `#/photos/review?photo_id=${encodeURIComponent(p.id)}`;
    } catch (e) { toast("Error: " + e.message); }
    setSending(false);
  };
  useEffect(() => {
    setDetail(null); setTimeline(null); setZoom(false);
    photoDetail(p.id).then(setDetail).catch(() => {});
    if (signedIn()) api(`/api/analytics/photos/${p.id}`).then(setTimeline).catch(() => {});
    if (index >= photos.length - 5) onNearEnd?.();
  }, [p.id]);
  useEffect(() => {
    const key = (e) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowRight" && index < photos.length - 1) onIndex(index + 1);
      else if (e.key === "ArrowLeft" && index > 0) onIndex(index - 1);
      else if (e.key === "z") setZoom((z) => !z);
      else if (e.key === "i") setInfo((v) => !v);
      else if (e.key === "s") play(!playing);
      else if (e.key === " " && playing) { e.preventDefault(); setHeld(false); play(false); }
    };
    document.addEventListener("keydown", key);
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", key); document.body.style.overflow = ""; };
  }, [index, photos.length, playing]);

  // The slideshow: the next photo after `secs`, from when this one is shown (so ← → restart the wait); at the end, it stops.
  const play = (on) => {
    setPlaying(on); setIdle(false);
    if (!on && full.current) { full.current = false; document.fullscreenElement && document.exitFullscreen().catch(() => {}); }
  };
  useEffect(() => { if (autoplay) play(true); }, []);
  useEffect(() => () => { full.current && document.fullscreenElement && document.exitFullscreen().catch(() => {}); }, []);
  useEffect(() => {
    if (!playing || held) return;
    const t = setTimeout(() => (index < photos.length - 1 ? onIndex(index + 1) : play(false)), secs * 1000);
    return () => clearTimeout(t);
  }, [playing, held, index, secs, photos.length]);
  useEffect(() => {
    const vis = () => setHeld(document.hidden);
    document.addEventListener("visibilitychange", vis);
    return () => document.removeEventListener("visibilitychange", vis);
  }, []);
  useEffect(() => {                                 // hide the controls after a moment's stillness
    if (!playing || idle) return;
    const t = setTimeout(() => setIdle(true), 2500);
    return () => clearTimeout(t);
  });
  const wake = () => idle && setIdle(false);
  const pace = (n) => { setSecs(n); try { localStorage.setItem(PACE_KEY, n); } catch {} };
  const fullScreen = () => {
    if (document.fullscreenElement) { full.current = false; document.exitFullscreen().catch(() => {}); return; }
    root.current?.requestFullscreen?.().then(() => { full.current = true; }).catch(() => {});
  };
  const touch = useTouch({ onPrev: index > 0 && (() => onIndex(index - 1)), onNext: index < photos.length - 1 && (() => onIndex(index + 1)),
                          onClose, reset: p.id, off: zoom });
  const { tf } = touch;
  const prefs = zoom || tf.s > 1.5 ? ZOOMED : SHOWN;
  const big = pick(p.sizes, prefs);
  // Once this image is in, fetch the neighbours' (the next two the way we're going, one the other way) so the next
  // arrow press shows a photo at once (#131). Never the zoom sizes: they're large, and the next photo opens unzoomed.
  const preload = () => {
    const step = index < was.current ? -1 : 1;
    was.current = index;
    if (frugal()) return;
    for (const i of [index + step, index - step, index + 2 * step]) {
      const n = photos[i];
      if (!n) continue;
      const img = new Image();
      img.decoding = "async";
      img.src = pick(n.sizes, SHOWN);
      photoDetail(n.id).catch(() => {});
    }
  };
  const d = detail, e = d?.engagement;
  const daily = timeline?.daily || [];
  return html`<div class=${"lightbox" + (info && !playing ? "" : " noinfo") + (playing ? " playing" : "") + (idle ? " idle" : "")}
      ref=${root} onPointerMove=${wake} onPointerDown=${wake} role="dialog" aria-modal="true" aria-label=${p.title || "Photo"}>
    <div class=${"stage" + (zoom ? " zoomed" : "")} ...${touch.handlers}>
      <img key=${big} src=${big} alt=${p.title || ""} onClick=${() => touch.touched() || setZoom(!zoom)} onLoad=${preload}
           onError=${fallback(p.sizes, prefs)} class=${tf.live ? "live" : ""}
           style=${{ transform: tf.s === 1 && !tf.x && !tf.y ? "" : `translate(${tf.x}px, ${tf.y}px) scale(${tf.s})`,
                     opacity: tf.s === 1 && tf.y > 0 ? Math.max(0.4, 1 - tf.y / 400) : "" }} />
      <button class="close" onClick=${onClose} aria-label="Close">✕</button>
      ${index > 0 && html`<button class="navbtn prev" onClick=${() => onIndex(index - 1)} aria-label="Previous">‹</button>`}
      ${index < photos.length - 1 && html`<button class="navbtn next" onClick=${() => onIndex(index + 1)} aria-label="Next">›</button>`}
      <div class="showbar" onPointerEnter=${() => setHeld(true)} onPointerLeave=${() => setHeld(document.hidden)}>
        <button onClick=${() => play(!playing)} aria-pressed=${playing} title="Slideshow (S)">${playing ? "❚❚ Pause" : "▶ Slideshow"}</button>
        ${playing && html`<select value=${secs} onChange=${(e) => pace(+e.target.value)} aria-label="Seconds per photo">
            ${PACES.map((n) => html`<option value=${n}>${n} s</option>`)}</select>
          ${document.fullscreenEnabled && html`<button onClick=${fullScreen} title="Full screen">⛶</button>`}`}
      </div>
    </div>
    ${info && !playing && html`<aside>
      <h2>${p.title || "Untitled"}</h2>
      ${d?.description && html`<p class="desc">${plain(d.description)}</p>`}
      <div class="stats3"><div><b>${num(p.views)}</b><span>views</span></div><div><b>${num(p.faves)}</b><span>faves</span></div>
        <div><b>${num(p.comments)}</b><span>comments</span></div></div>
      ${daily.length > 1 && html`<${Chart} height=${120} days=${daily.map((r) => r.day)}
          series=${[{ label: "views/day", values: daily.map((r) => r.views), color: "--chart-1" }]} />`}
      ${d && html`<dl>
        ${d.date_taken && html`<dt>Taken</dt><dd><a href=${photosLink({ year: d.date_taken.slice(0, 4) })} onClick=${onClose}
            title="All photos taken that year">${d.date_taken.slice(0, 10)}</a></dd>`}
        ${d.camera && html`<dt>Camera</dt><dd><a href=${photosLink({ camera: d.camera })} onClick=${onClose}
            title="All photos from this camera">${d.camera}</a></dd>`}
        ${d.lens && html`<dt>Lens</dt><dd>${d.lens}</dd>`}
        ${d.exif?.ExposureTime && html`<dt>Exposure</dt><dd>${[d.exif.ExposureTime, d.exif.FNumber, d.exif.ISO && "ISO " + d.exif.ISO, d.exif.FocalLengthIn35mmFormat || d.exif.FocalLength].filter(Boolean).join(" · ")}</dd>`}
        ${d.width && html`<dt>Size</dt><dd>${num(d.width)} × ${num(d.height)}</dd>`}
        ${d.albums.length > 0 && html`<dt>Albums</dt><dd>${d.albums.map((a, i) => html`${i ? ", " : ""}<a href=${photosLink({ album: a.id })} onClick=${onClose}>${a.title}</a>`)}</dd>`}
        ${e?.groups.length > 0 && html`<dt>Groups</dt><dd>${e.groups.length}</dd>`}
      </dl>`}
      ${d?.lat != null && d?.lon != null && !(d.lat === 0 && d.lon === 0) && html`<${MiniMap} lat=${d.lat} lon=${d.lon} />
        <p class="maplink"><a href=${`#/photos/map/@${d.lat},${d.lon},14`} onClick=${onClose}>Show on the map</a></p>`}
      ${d?.tags.length > 0 && html`<div class="chips">${d.tags.map((t) => html`<a class="chip" href=${photosLink({ tag: t })}
          onClick=${onClose} title=${`All photos tagged ${t}`}>${t}</a>`)}</div>`}
      ${browsing && html`<p><button class="link" onClick=${onBack}>← Back</button> <span class="muted">browsing ${browsing.toLowerCase()}</span></p>`}
      <${Related} id=${p.id} onPick=${onRelated} onClose=${onClose} />
      ${e?.recent_comments.length > 0 && html`<h3>Latest comments</h3>${e.recent_comments.map((c) => html`<div class="comment">
        <div class="who">${c.real_name || c.author_name} · ${day(c.date_created)}</div>${plain(c.text)}</div>`)}`}
      ${d && html`<p>${p.ispublic === 1 && html`<button onClick=${() => share(`/p/${p.id}`, p.title || "Photo")}>Share</button> `}
        <a href=${d.url} target="_blank" rel="noopener noreferrer">Open on Flickr ↗</a></p>`}
      ${signedIn() && html`<p><button onClick=${sendToReview} disabled=${sending}>${sending ? "Sending…" : "Send to Review"}</button></p>`}
      <p class="hint"><kbd>←</kbd> <kbd>→</kbd> browse · <kbd>Z</kbd> zoom · <kbd>I</kbd> info · <kbd>S</kbd> slideshow · <kbd>Esc</kbd> close</p>
    </aside>`}
  </div>`;
}
