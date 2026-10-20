// Photostream, albums, and the lightbox (zoom, keyboard navigation, info panel).
import { html, useState, useEffect, useRef, useCallback, api, num, day, plain, pick, fallback, go } from "./lib.js";

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

export function Photostream() {
  const [q, setQ] = useState(""), [query, setQuery] = useState("");
  const [sort, setSort] = useState("newest"), [media, setMedia] = useState("");
  useEffect(() => { const t = setTimeout(() => setQuery(q.trim()), 300); return () => clearTimeout(t); }, [q]);
  const paged = usePaged((offset) => api("/api/photos", { params: { q: query, sort, media, offset, limit: PAGE } }),
                         [query, sort, media]);
  return html`
    <div class="toolbar">
      <input type="search" placeholder="Search titles, descriptions, tags" value=${q}
             onInput=${(e) => setQ(e.target.value)} aria-label="Search" />
      <select value=${sort} onChange=${(e) => setSort(e.target.value)} aria-label="Sort">
        <option value="newest">Newest uploads</option><option value="oldest">Oldest uploads</option>
        <option value="taken">Date taken</option><option value="views">Most viewed</option>
        <option value="faves">Most faved</option><option value="comments">Most commented</option>
      </select>
      <select value=${media} onChange=${(e) => setMedia(e.target.value)} aria-label="Media">
        <option value="">Photos and videos</option><option value="photo">Photos</option><option value="video">Videos</option>
      </select>
      <span class="muted">${paged.total !== null && `${num(paged.total)} items`}</span>
    </div>
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
      ${shown.map((a) => html`<a class="album" key=${a.id} href=${"#/albums/" + a.id}>
        <div class="cover">${a.cover_url && html`<img loading="lazy" alt="" src=${a.cover_url} />`}</div>
        <div class="meta"><b>${a.title}</b><span>${num(a.count)} items${a.date_update ? " · updated " + day(a.date_update) : ""}</span></div>
      </a>`)}
    </div>`;
}

export function AlbumView({ id }) {
  const [album, setAlbum] = useState(null);
  const [sort, setSort] = useState("newest");
  useEffect(() => { api(`/api/albums/${encodeURIComponent(id)}`, { params: { limit: 1 } }).then(setAlbum).catch(() => go("albums")); }, [id]);
  const paged = usePaged((offset) => api("/api/photos", { params: { album: id, sort, offset, limit: PAGE } }), [id, sort]);
  return html`
    <a href="#/albums">← Albums</a>
    <h1 class="title">${album ? album.title : "…"}</h1>
    ${album?.description && html`<p class="muted">${plain(album.description)}</p>`}
    <div class="toolbar">
      <select value=${sort} onChange=${(e) => setSort(e.target.value)} aria-label="Sort">
        <option value="newest">Newest uploads</option><option value="taken">Date taken</option>
        <option value="views">Most viewed</option><option value="faves">Most faved</option>
      </select>
      <span class="muted">${paged.total !== null && `${num(paged.total)} items`}</span>
    </div>
    <${PhotoGrid} paged=${paged} />`;
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
  return html`<div class="lightbox" role="dialog" aria-modal="true" aria-label=${p.title || "Photo"}
      style=${info ? "" : "grid-template-columns: 1fr"}>
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
        ${d.date_taken && html`<dt>Taken</dt><dd>${d.date_taken.slice(0, 10)}</dd>`}
        ${d.camera && html`<dt>Camera</dt><dd>${d.camera}</dd>`}
        ${d.lens && html`<dt>Lens</dt><dd>${d.lens}</dd>`}
        ${d.exif?.ExposureTime && html`<dt>Exposure</dt><dd>${[d.exif.ExposureTime, d.exif.FNumber, d.exif.ISO && "ISO " + d.exif.ISO, d.exif.FocalLengthIn35mmFormat || d.exif.FocalLength].filter(Boolean).join(" · ")}</dd>`}
        ${d.width && html`<dt>Size</dt><dd>${num(d.width)} × ${num(d.height)}</dd>`}
        ${d.albums.length > 0 && html`<dt>Albums</dt><dd>${d.albums.map((a, i) => html`${i ? ", " : ""}<a href=${"#/albums/" + a.id} onClick=${onClose}>${a.title}</a>`)}</dd>`}
        ${e?.groups.length > 0 && html`<dt>Groups</dt><dd>${e.groups.length}</dd>`}
      </dl>`}
      ${d?.lat != null && d?.lon != null && !(d.lat === 0 && d.lon === 0) && html`<${MiniMap} lat=${d.lat} lon=${d.lon} />
        <p class="maplink"><a href=${`#/map/@${d.lat},${d.lon},14`} onClick=${onClose}>Show on the map</a></p>`}
      ${d?.tags.length > 0 && html`<div class="chips">${d.tags.map((t) => html`<span class="chip">${t}</span>`)}</div>`}
      ${e?.recent_comments.length > 0 && html`<h3>Latest comments</h3>${e.recent_comments.map((c) => html`<div class="comment">
        <div class="who">${c.real_name || c.author_name} · ${day(c.date_created)}</div>${plain(c.text)}</div>`)}`}
      ${d && html`<p><a href=${d.url} target="_blank" rel="noopener noreferrer">Open on Flickr ↗</a></p>`}
      <p class="hint"><kbd>←</kbd> <kbd>→</kbd> browse · <kbd>Z</kbd> zoom · <kbd>I</kbd> info · <kbd>Esc</kbd> close</p>
    </aside>`}
  </div>`;
}
