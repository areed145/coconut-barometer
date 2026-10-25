// Photos (one page for every filtered view: album, tag, year, camera, search), albums, and the lightbox.
import { html, useState, useEffect, useRef, useMemo, useCallback, api, num, day, plain, pick, fallback, photosLink, toast, signedIn, share, today, dayLabel } from "./lib.js";

const THUMB = ["n", "w", "m", "q"];
import { Chart } from "./chart.js";
import { MiniMap } from "./leaflet.js";
import { aspect, justify } from "./justify.js";

const PAGE = 60;

// Loads pages from fetchPage(offset) as the sentinel scrolls into view.
function usePaged(fetchPage, deps) {
  const [photos, setPhotos] = useState([]);
  const [total, setTotal] = useState(null);
  const [loading, setLoading] = useState(false);
  const state = useRef({ photos: [], total: null, loading: false, gen: 0 });
  const more = useCallback(async () => {
    const s = state.current;
    if (s.loading || (s.total !== null && s.photos.length >= s.total)) return;
    s.loading = true; setLoading(true);
    const gen = s.gen;
    try {
      const page = await fetchPage(s.photos.length);
      if (gen !== state.current.gen) return;           // filters changed meanwhile
      s.photos = [...s.photos, ...page.photos]; s.total = page.total;
      setPhotos(s.photos); setTotal(s.total);
    } finally { if (gen === state.current.gen) { s.loading = false; setLoading(false); } }
  }, deps);
  useEffect(() => {
    state.current = { photos: [], total: null, loading: false, gen: state.current.gen + 1 };
    setPhotos([]); setTotal(null);
    more();
  }, deps);
  return { photos, total, loading, more };
}

export function PhotoGrid({ paged }) {
  const { photos, total, loading, more } = paged;
  const [open, setOpen] = useState(null);           // index into photos
  const sentinel = useRef(null);
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
  if (total === 0) return html`<div class="empty">No photos here.</div>`;
  return html`
    <div class="justified" ref=${box}>
      ${rows.map((r) => html`<div class=${"jrow" + (r.last ? " last" : "")} key=${photos[r.start].id}>
        ${photos.slice(r.start, r.end).map((p, j) => {
          const w = aspect(p) * r.height;
          return html`<button class="tile" key=${p.id} onClick=${() => setOpen(r.start + j)} aria-label=${p.title || "Untitled photo"}
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
    <div class="more">${loading ? "Loading…" : total !== null && `${num(photos.length)} of ${num(total)}`}</div>
    ${open !== null && html`<${Lightbox} photos=${photos} index=${open} onIndex=${setOpen}
        onClose=${() => setOpen(null)} onNearEnd=${more} />`}`;
}

const SORTS = [["newest", "Newest uploads"], ["oldest", "Oldest uploads"], ["taken", "Date taken"], ["views", "Most viewed"],
               ["faves", "Most faved"], ["comments", "Most commented"], ["title", "Title"]];
const FILTERS = ["album", "tag", "year", "on", "camera", "media", "q", "sort", "is"];

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
  const paged = usePaged((offset) => api("/api/photos", { params: { album: f.album, tag: f.tag, year: f.year, on: f.on, camera: f.camera,
    media: f.media, q: f.q, sort: f.sort || "newest", is: f.is, offset, limit: PAGE } }), [key]);
  const chips = [["album", "Album", album?.title || "…"], ["tag", "Tag", f.tag], ["year", "Taken in", f.year],
                 ["on", "On this day", f.on && dayLabel(f.on)], ["camera", "Camera", f.camera]]
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
    <${PhotoGrid} paged=${paged} />`;
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

export function Lightbox({ photos, index, onIndex, onClose, onNearEnd }) {
  const p = photos[index];
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
    };
    document.addEventListener("keydown", key);
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", key); document.body.style.overflow = ""; };
  }, [index, photos.length]);
  const prefs = zoom ? ["k", "h", "l", "b", "c", "z", "w", "n"] : ["b", "l", "c", "z", "w", "n"];
  const big = pick(p.sizes, prefs);
  // Once this image is in, fetch the neighbours' (the next two the way we're going, one the other way) so the next
  // arrow press shows a photo at once (#131). Not the zoom sizes unless zoomed: they're large.
  const preload = () => {
    const step = index < was.current ? -1 : 1;
    was.current = index;
    if (frugal()) return;
    for (const i of [index + step, index - step, index + 2 * step]) {
      const n = photos[i];
      if (!n) continue;
      const img = new Image();
      img.decoding = "async";
      img.src = pick(n.sizes, prefs);
      photoDetail(n.id).catch(() => {});
    }
  };
  const d = detail, e = d?.engagement;
  const daily = timeline?.daily || [];
  return html`<div class=${"lightbox" + (info ? "" : " noinfo")} role="dialog" aria-modal="true" aria-label=${p.title || "Photo"}>
    <div class=${"stage" + (zoom ? " zoomed" : "")}>
      <img key=${big} src=${big} alt=${p.title || ""} onClick=${() => setZoom(!zoom)} onLoad=${preload}
           onError=${fallback(p.sizes, prefs)} />
      <button class="close" onClick=${onClose} aria-label="Close">✕</button>
      ${index > 0 && html`<button class="navbtn prev" onClick=${() => onIndex(index - 1)} aria-label="Previous">‹</button>`}
      ${index < photos.length - 1 && html`<button class="navbtn next" onClick=${() => onIndex(index + 1)} aria-label="Next">›</button>`}
    </div>
    ${info && html`<aside>
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
      ${e?.recent_comments.length > 0 && html`<h3>Latest comments</h3>${e.recent_comments.map((c) => html`<div class="comment">
        <div class="who">${c.real_name || c.author_name} · ${day(c.date_created)}</div>${plain(c.text)}</div>`)}`}
      ${d && html`<p>${p.ispublic === 1 && html`<button onClick=${() => share(`/p/${p.id}`, p.title || "Photo")}>Share</button> `}
        <a href=${d.url} target="_blank" rel="noopener noreferrer">Open on Flickr ↗</a></p>`}
      ${signedIn() && html`<p><button onClick=${sendToReview} disabled=${sending}>${sending ? "Sending…" : "Send to Review"}</button></p>`}
      <p class="hint"><kbd>←</kbd> <kbd>→</kbd> browse · <kbd>Z</kbd> zoom · <kbd>I</kbd> info · <kbd>Esc</kbd> close</p>
    </aside>`}
  </div>`;
}
