// Gear (#127): the cameras and lenses behind the photos, when each was in use, and the settings they were used at.
// Every bar and row links to the photostream filtered to it.
import { html, useState, useEffect, api, num, photosLink } from "./lib.js";
import { Bars, Kpi, Table } from "./viz.js";

const SETTINGS = [["focal", "Focal length (35 mm equivalent)"], ["f", "Aperture"], ["shutter", "Shutter speed"], ["iso", "ISO"]];
const go = (params) => { location.hash = photosLink({ ...params, sort: "taken" }); };
const year = (d) => (d ? +d.slice(0, 4) : null);

// When each camera was in use: a bar from its first photo to its last, on one scale of years.
function InUse({ rows }) {
  const dated = rows.filter((r) => r.first && r.last);
  if (!dated.length) return html`<p class="muted">No dates.</p>`;
  const lo = Math.min(...dated.map((r) => year(r.first))), hi = Math.max(...dated.map((r) => year(r.last))) + 1;
  const at = (d) => (100 * (year(d) + (+d.slice(5, 7) - 1) / 12 - lo)) / (hi - lo);
  const ticks = [];
  for (let y = lo; y <= hi; y += Math.max(1, Math.ceil((hi - lo) / 8))) ticks.push(y);
  return html`<div class="inuse">
    ${dated.map((r) => html`<button class="link row" key=${r.name} onClick=${() => go({ camera: r.name })}
        title=${`${r.name}: ${num(r.photos)} photos, ${r.first} to ${r.last}`}>
      <span class="label">${r.name}</span>
      <span class="track"><span class="span" style=${{ left: `${at(r.first)}%`, width: `${Math.max(0.8, at(r.last) - at(r.first) + 100 / 12 / (hi - lo))}%` }}></span></span>
      <span class="n">${num(r.photos)}</span>
    </button>`)}
    <div class="row axis"><span></span><span class="track">${ticks.map((y) => html`<span key=${y} style=${{ left: `${(100 * (y - lo)) / (hi - lo)}%` }}>${y}</span>`)}</span><span></span></div>
  </div>`;
}

export function Gear() {
  const [g, setG] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => { api("/api/gear").then(setG).catch((e) => setErr(e.message)); }, []);
  if (err) return html`<div class="empty">Couldn't load the gear: ${err}</div>`;
  if (!g) return html`<div class="boot">Loading…</div>`;
  if (!g.cameras.length) return html`<div class="empty">No camera details yet: they come with the photos' EXIF, fetched after a sync.</div>`;
  const per = (r) => `${num(r.photos)} photos · ${r.views.toFixed(0)} views and ${r.faves.toFixed(1)} faves each on average`;
  const engagement = [{ key: "name", label: "Name" }, { key: "photos", label: "Photos", num: true },
    { key: "views", label: "Views each", num: true, fmt: (v) => num(Math.round(v)) }, { key: "faves", label: "Faves each", num: true, fmt: (v) => v.toFixed(1) },
    { key: "first", label: "First" }, { key: "last", label: "Last" }];
  return html`
    <div class="kpis"><${Kpi} label="Cameras" now=${g.cameras.length} /><${Kpi} label="Lenses" now=${g.lenses.length} />
      <${Kpi} label="Photos with camera details" now=${g.photos} /></div>
    <div class="card gear"><h2>Cameras in use</h2><${InUse} rows=${g.cameras} /></div>
    <div class="cols settings4">
      ${SETTINGS.map(([k, title]) => html`<div class="card" key=${k}><h2>${title}</h2>
        <${Bars} rows=${g.settings[k]} value="photos" onPick=${(r) => go({ setting: `${k}:${r.low}-${r.high ?? ""}` })}
          title=${(r) => `${num(r.photos)} photos: see them`} empty="No photos with this setting." /></div>`)}
    </div>
    <div class="cols">
      <div class="card"><h2>Lenses</h2>
        <${Bars} rows=${g.lenses.slice(0, 15).map((r) => ({ ...r, label: r.name }))} value="photos" onPick=${(r) => go({ lens: r.name })}
          title=${per} /></div>
      <div class="card"><h2>Camera and lens</h2>
        <${Bars} rows=${g.pairs.slice(0, 15).map((r) => ({ ...r, label: `${r.camera} · ${r.lens}` }))} value="photos"
          onPick=${(r) => go({ camera: r.camera, lens: r.lens })} /></div>
    </div>
    <div class="card"><h2>How each camera's photos do</h2>
      <${Table} rows=${g.cameras} cols=${engagement} initial="photos" onPick=${(r) => go({ camera: r.name })} /></div>
    <div class="card"><h2>How each lens's photos do</h2>
      <${Table} rows=${g.lenses} cols=${engagement} initial="photos" onPick=${(r) => go({ lens: r.name })} /></div>`;
}
