// Photos (one page for every filtered view: album, tag, year, camera, search), albums, and the lightbox.
import { html, useState, useEffect, useRef, useCallback, api, num, day, plain, pick, fallback, photosLink } from "./lib.js";

const THUMB = ["n", "w", "m", "q"];
import { Chart } from "./chart.js";
import { MiniMap } from "./leaflet.js";

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
  useEffect(() => {
    const io = new IntersectionObserver((e) => e[0].isIntersecting && more(), { rootMargin: "800px" });
    if (sentinel.current) io.observe(sentinel.current);
    return () => io.disconnect();
  }, [more]);
  if (total === 0) return html`<div class="empty">No photos here.</div>`;
  return html`
    <div class="grid">
      ${photos.map((p, i) => html`<button class="tile" key=${p.id} onClick=${() => setOpen(i)}
          aria-label=${p.title || "Untitled photo"}>
        <img loading="lazy" decoding="async" alt="" src=${pick(p.sizes, THUMB)} onError=${fallback(p.sizes, THUMB)}
             srcset=${[p.sizes.n && p.sizes.n + " 320w", p.sizes.w && p.sizes.w + " 400w", p.sizes.z && p.sizes.z + " 640w"].filter(Boolean).join(", ")}
             sizes="(max-width: 520px) 34vw, 200px" />
        ${p.media === "video" && html`<span class="vid">video</span>`}
        <span class="cap">${p.title || "Untitled"}<br />${num(p.views)} views · ${num(p.faves)} faves</span>
      </button>`)}
    </div>
    <div class="sentinel" ref=${sentinel}></div>
    <div class="more">${loading ? "Loading…" : total !== null && `${num(photos.length)} of ${num(total)}`}</div>
    ${open !== null && html`<${Lightbox} photos=${photos} index=${open} onIndex=${setOpen}
        onClose=${() => setOpen(null)} onNearEnd=${more} />`}`;
}

const SORTS = [["newest", "Newest uploads"], ["oldest", "Oldest uploads"], ["taken", "Date taken"], ["views", "Most viewed"],
               ["faves", "Most faved"], ["comments", "Most commented"], ["title", "Title"]];
const FILTERS = ["album", "tag", "year", "camera", "media", "q", "sort", "is"];

// The Photos page, and every filtered view of it: the filters live in the address
// (#/photos?album=…&tag=…&year=…&camera=…&media=…&q=…&sort=…), so any of them can be linked to.
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
  const paged = usePaged((offset) => api("/api/photos", { params: { album: f.album, tag: f.tag, year: f.year, camera: f.camera,
    media: f.media, q: f.q, sort: f.sort || "newest", is: f.is, offset, limit: PAGE } }), [key]);
  const chips = [["album", "Album", album?.title || "…"], ["tag", "Tag", f.tag], ["year", "Taken in", f.year], ["camera", "Camera", f.camera]]
    .filter(([k]) => f[k]);
  const label = Object.fromEntries(presets.map((p) => [p.id, p.label]));
  const groups = [...new Set(presets.map((p) => p.group))];
  return html`
    ${f.album && html`<a href="#/albums">← Albums</a>
      <h1 class="title">${album ? album.title : "…"}</h1>
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

export function Lightbox({ photos, index, onIndex, onClose, onNearEnd }) {
  const p = photos[index];
  const [detail, setDetail] = useState(null);
  const [timeline, setTimeline] = useState(null);
  const [zoom, setZoom] = useState(false);
  const [info, setInfo] = useState(true);
  useEffect(() => {
    setDetail(null); setTimeline(null); setZoom(false);
    api(`/api/photos/${p.id}`).then(setDetail).catch(() => {});
    api(`/api/analytics/photos/${p.id}`).then(setTimeline).catch(() => {});
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
  const d = detail, e = d?.engagement;
  const daily = timeline?.daily || [];
  return html`<div class=${"lightbox" + (info ? "" : " noinfo")} role="dialog" aria-modal="true" aria-label=${p.title || "Photo"}>
    <div class=${"stage" + (zoom ? " zoomed" : "")}>
      <img key=${big} src=${big} alt=${p.title || ""} onClick=${() => setZoom(!zoom)} onError=${fallback(p.sizes, prefs)} />
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
        <p class="maplink"><a href=${`#/map/@${d.lat},${d.lon},14`} onClick=${onClose}>Show on the map</a></p>`}
      ${d?.tags.length > 0 && html`<div class="chips">${d.tags.map((t) => html`<a class="chip" href=${photosLink({ tag: t })}
          onClick=${onClose} title=${`All photos tagged ${t}`}>${t}</a>`)}</div>`}
      ${e?.recent_comments.length > 0 && html`<h3>Latest comments</h3>${e.recent_comments.map((c) => html`<div class="comment">
        <div class="who">${c.real_name || c.author_name} · ${day(c.date_created)}</div>${plain(c.text)}</div>`)}`}
      ${d && html`<p><a href=${d.url} target="_blank" rel="noopener noreferrer">Open on Flickr ↗</a></p>`}
      <p class="hint"><kbd>←</kbd> <kbd>→</kbd> browse · <kbd>Z</kbd> zoom · <kbd>I</kbd> info · <kbd>Esc</kbd> close</p>
    </aside>`}
  </div>`;
}
