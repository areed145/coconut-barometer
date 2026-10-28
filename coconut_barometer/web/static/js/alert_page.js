// An alert's own page (#73), where its notification opens: #/alerts/<when>/<key>. What it said and why, how it went
// to each device (or why it was held back), and the evidence as it stood then: a gust or pressure alert's chart
// around that moment, a storm or rain alert's radar map from the frame kept nearest it (frames are kept for about
// two hours, so an older one says so). A push notification can't carry a picture on iPhone, so this is where it is.
import { html, useState, useEffect, useMemo, useRef, api } from "./lib.js";
import { unitsFor, show } from "./units.js";
import { TimeChart } from "./weather.js";
import { leaflet, baseMap } from "./leaflet.js";
import { decode, radarCanvas, levelColor } from "./radar_layer.js";

const HOUR = 3600;
const when = (ts) => new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
const LABELS = { device_id: null, link: null, latitude: null, longitude: null, track: null, cell_id: "cell" };
const label = (k) => (k in LABELS ? LABELS[k] : k.replaceAll("_", " "));
// (lat, lon) moved `km` toward `deg` (degrees from north): near enough over an hour's travel.
const ahead = (lat, lon, deg, km) => {
  const a = (deg * Math.PI) / 180;
  return [lat + (km * Math.cos(a)) / 111.2, lon + (km * Math.sin(a)) / (111.2 * Math.cos((lat * Math.PI) / 180))];
};

// A gust or pressure alert: that station's chart from 6 hours before to an hour after, the alert's moment shaded.
function AlertChart({ alert, kind }) {
  const [data, setData] = useState(null);
  const dev = alert.evidence.device_id;
  useEffect(() => {
    api("/api/wx/history", { params: { device: dev, start: alert.at - 6 * HOUR, end: alert.at + HOUR } })
      .then(setData).catch(() => setData({ error: true }));
  }, [alert.at, dev]);
  const chart = useMemo(() => {
    const d = data?.devices?.[dev];
    if (!d) return null;
    const u = unitsFor(data.units), conv = (k, v) => (v == null ? null : u[k][1](v));
    const marks = [{ color: "--warn", values: data.t.map((t) => Math.abs(t - alert.at) <= data.bucket / 2 + 60) }];
    return kind === "gust"
      ? { unit: u.speed[0], marks, series: [{ label: "Gust", color: "--chart-1", values: d.wind_gust.map((v) => conv("speed", v)) },
          { label: "Average", color: "--chart-2", thin: true, values: d.wind_avg.map((v) => conv("speed", v)) }] }
      : { unit: u.pressure[0], marks, series: [{ label: "Pressure (sea level)", color: "--chart-1", digits: data.units === "imperial" ? 2 : 1,
          values: d.pressure.map((v) => conv("pressure", v)) }] };
  }, [data]);
  if (data?.error) return html`<p class="muted">The station's history couldn't be read.</p>`;
  if (!chart) return html`<p class="muted">Loading the chart…</p>`;
  return html`<div class="card wide"><h2>${kind === "gust" ? "Wind" : "Pressure"} around the alert</h2>
    <${TimeChart} t=${data.t} series=${chart.series} unit=${chart.unit} marks=${chart.marks} step=${data.bucket} />
    <p class="muted">Shaded: the alert. From 6 hours before to an hour after.</p></div>`;
}

// A storm or rain alert: the station, the radar frame kept nearest the alert, and the storm's track or the rain's
// distance then.
function AlertMap({ alert, kind }) {
  const box = useRef(null);
  const [note, setNote] = useState("");
  const e = alert.evidence;
  useEffect(() => {
    let map = null, alive = true;
    (async () => {
      const [lf, stations, frames, now] = await Promise.all([leaflet(), api("/api/wx/map"), api("/api/wx/radar/frames"),
        api("/api/wx/radar?zoom=4").catch(() => null)]);
      if (!alive || !box.current) return;
      const L = lf.L, st = stations.stations.find((s) => s.device_id === e.device_id);
      if (!st) { setNote("That station is no longer on the map."); return; }
      map = baseMap(lf, box.current, { minZoom: 4 }).setView([st.lat, st.lon], 8);
      map.createPane("wxradar").style.zIndex = 360;
      L.circleMarker([st.lat, st.lon], { radius: 6, color: getComputedStyle(document.documentElement).getPropertyValue("--fg"), weight: 2,
        fillOpacity: 1 }).bindTooltip(st.name).addTo(map);
      if (kind === "storm") {
        if (e.radius_km) L.circle([st.lat, st.lon], { radius: e.radius_km * 1000, weight: 1, dashArray: "4 4", fill: false }).bindTooltip("alert radius").addTo(map);
        if (e.latitude != null) {
          const path = [[e.latitude, e.longitude], ahead(e.latitude, e.longitude, e.direction_degrees || 0, e.speed_kmh || 0)];
          if (e.speed_kmh) L.polyline(path, { weight: 2, dashArray: "4 6" }).bindTooltip("its course for the next hour, as scanned").addTo(map);
          L.circleMarker(path[0], { radius: 8, weight: 2, fillOpacity: 0.9 }).bindTooltip(`${e.dbz} dBZ storm${e.radar_observed_at ? `, scanned ${when(e.radar_observed_at)}` : ""}`).addTo(map);
          map.fitBounds([[st.lat, st.lon], ...path], { padding: [40, 40], maxZoom: 9 });
        }
      } else if (e.distance_km) {
        L.circle([st.lat, st.lon], { radius: e.distance_km * 1000, weight: 1, dashArray: "4 4", fill: false })
          .bindTooltip("the nearest rain then").addTo(map);
      }
      // The radar frame kept nearest the alert (within 10 minutes before it).
      const t = frames.frames.map((f) => f.t).filter((x) => x <= alert.at + 60 && alert.at - x <= 600).pop();
      if (t == null) { setNote("The radar from then is no longer kept (about two hours are)."); return; }
      const b = map.getBounds().pad(0.5);
      const frame = await api(`/api/wx/radar/frame?t=${t}&zoom=8&bbox=${[b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((v) => v.toFixed(2)).join(",")}`);
      if (!alive) return;
      radarCanvas(L, "wxradar").addTo(map).set(decode(frame.shapes), (dbz) => levelColor(now?.colors, dbz)).setOpacity(0.85);
      setNote(`Radar: NOAA MRMS at ${when(t)}.`);
    })().catch(() => setNote("The map couldn't load."));
    return () => { alive = false; map?.remove(); };
  }, [alert.at, alert.key]);
  return html`<div class="card wide"><h2>${kind === "storm" ? "The storm" : "The rain"} when the alert went</h2>
    <div class="mapwrap alertmap" ref=${box}></div>${note && html`<p class="muted">${note}</p>`}</div>`;
}

export function AlertPage({ at, alertKey }) {
  const [alert, setAlert] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    setAlert(null);
    api("/api/push/alert", { params: { at, key: decodeURIComponent(alertKey || "") } }).then(setAlert)
      .catch((e) => setErr(e.status === 404 ? "This alert isn't in the log any more (the newest 1,000 are kept)." : e.message));
  }, [at, alertKey]);
  if (err) return html`<p class="problem">${err}</p>`;
  if (!alert) return html`<div class="boot">Loading…</div>`;
  const kind = alert.key.split(":")[0], e = alert.evidence;
  const held = alert.deliveries.find((d) => d.status === "suppressed");
  const rows = Object.entries(e).filter(([k, v]) => label(k) && v != null && v !== "");
  return html`
    <a href="#/alerts">← Alerts</a>
    <h1 class="title">${alert.title}</h1>
    <p>${alert.body}</p>
    <p class="muted">${when(alert.at)} · ${held ? html`<b class="problem">held back</b>: ${held.detail}`
      : alert.deliveries.map((d) => `${d.device_label}: ${d.status}${d.detail ? ` (${d.detail})` : ""}`).join(" · ")}</p>
    ${(kind === "gust" || kind === "pressure") && html`<${AlertChart} alert=${alert} kind=${kind} />`}
    ${(kind === "storm" || kind === "rain") && html`<${AlertMap} alert=${alert} kind=${kind} />`}
    <div class="card"><h2>Evidence</h2>
      <div class="alert-evidence">${rows.map(([k, v]) => html`<span key=${k}>${label(k)}: ${
        typeof v === "number" ? (k.endsWith("_at") || k.startsWith("window_") ? when(v) : Number.isInteger(v) ? v : v.toFixed(2)) : String(v)}</span>`)}</div>
      ${e.link && html`<p><a href=${e.link}>${kind === "callsign" || kind === "place" ? "Open APRS" : ["altitude", "igate", "record"].includes(kind) ? "Open the igate" : kind === "storm" || kind === "rain" ? "Open the weather map"
        : "Open the full chart"} →</a></p>`}
    </div>`;
}
