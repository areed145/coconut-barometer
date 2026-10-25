// The Weather map's controls (#78), for a phone and a desktop alike: my location (and following it), full screen,
// the Layers panel (a floating panel, a bottom sheet on a phone) and the timeline bar along the bottom of the map.
// Leaflet's own controls are built as elements; the rest is Preact over the map. No HTML strings with styles (CSP).
import { html, useState, useEffect, useRef, toast } from "./lib.js";

// ---- remembered per device ------------------------------------------------------------------------------------
const PREFS = "cb-wxmap-layers", FOLLOW = "cb-wxmap-follow";
export const DEFAULT_LAYERS = { radar: true, storms: true, hazards: true, hazardsMinor: false, fronts: false, glm: true,
  lightningStyle: "smooth", rings: true, temp: false, wind: false, sat: "", radarOpacity: 0.85, satOpacity: 0.9, tempOpacity: 0.6, basemap: "auto" };
export function loadLayers() {
  try {
    const saved = JSON.parse(localStorage.getItem(PREFS) || "{}");
    // The stations' strike rings were "lightning" until GOES lightning came (#94): their choice carries over.
    if (saved.rings === undefined && typeof saved.lightning === "boolean") saved.rings = saved.lightning;
    // The lightning's style was glmStyle while blobs were the default; smooth shapes are now (#101), dots kept if chosen.
    if (saved.lightningStyle === undefined && saved.glmStyle === "dots") saved.lightningStyle = "dots";
    return Object.fromEntries(Object.entries(DEFAULT_LAYERS).map(([k, v]) => [k, typeof saved[k] === typeof v ? saved[k] : v]));
  } catch { return { ...DEFAULT_LAYERS }; }
}
export const saveLayers = (layers) => { try { localStorage.setItem(PREFS, JSON.stringify(layers)); } catch { /* this visit only */ } };
const followPref = () => { try { return localStorage.getItem(FOLLOW) !== "off"; } catch { return true; } };
const saveFollow = (on) => { try { localStorage.setItem(FOLLOW, on ? "on" : "off"); } catch { /* this visit only */ } };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// ⌖ and ⛶, in Leaflet's corner with the zoom buttons. handlers.current = {locate, full}, read on each click.
export function toolsControl(L, handlers) {
  const Tools = L.Control.extend({
    options: { position: "topleft" },
    onAdd() {
      const bar = L.DomUtil.create("div", "leaflet-bar wxtools");
      const button = (cls, label, text, fn) => {
        const b = el("button", cls, text);
        b.type = "button";
        b.title = label;
        b.setAttribute("aria-label", label);
        L.DomEvent.on(b, "click", (e) => { L.DomEvent.stop(e); fn(); });
        bar.append(b);
        return b;
      };
      this.locate = button("wxlocate", "Show my location", "⌖", () => handlers.current.locate());
      this.full = button("wxfullbtn", "Full screen", "⛶", () => handlers.current.full());
      L.DomEvent.disableClickPropagation(bar);
      L.DomEvent.disableScrollPropagation(bar);
      return bar;
    },
  });
  return new Tools();
}

// ---- my location ------------------------------------------------------------------------------------------------
// mode: off | finding | on (the dot, the map left alone) | follow (the map kept on it). Asked for only on a tap; the
// position stays in the browser. The watch stops while the tab is hidden and starts again on return.
export function useLocate(map, ready) {
  const [mode, setMode] = useState("off");
  const st = useRef({ watch: null, dot: null, ring: null, auto: false, first: false, last: null, mode: "off", hidden: false });
  st.current.mode = mode;

  const stop = () => {
    const s = st.current;
    if (s.watch != null) navigator.geolocation.clearWatch(s.watch);
    s.watch = null;
  };
  const clear = () => { stop(); st.current.dot?.remove(); st.current.ring?.remove(); st.current.dot = st.current.ring = null; };
  const move = (fn) => {                       // the map moved by us, not by hand: following stays on
    const m = map.current, s = st.current;
    s.auto = true;
    m.once("moveend", () => { s.auto = false; });
    fn(m);
  };
  const fix = (p) => {
    const m = map.current, s = st.current, L = window.L;
    if (!m) return;
    const at = [p.coords.latitude, p.coords.longitude];
    s.last = at;
    if (!s.dot) {
      s.ring = L.circle(at, { pane: "wxme", radius: p.coords.accuracy, color: "#2f80ed", weight: 1, fillColor: "#2f80ed", fillOpacity: 0.12, interactive: false }).addTo(m);
      s.dot = L.circleMarker(at, { pane: "wxme", radius: 7, color: "#fff", weight: 2, fillColor: "#2f80ed", fillOpacity: 1, interactive: false }).addTo(m);
    } else {
      s.ring.setLatLng(at).setRadius(p.coords.accuracy);
      s.dot.setLatLng(at);
    }
    if (s.first) {
      s.first = false;
      setMode(followPref() ? "follow" : "on");
      move((mm) => mm.flyTo(at, Math.max(mm.getZoom(), 9), { duration: 0.8 }));
    } else if (s.mode === "follow" && !m.getBounds().pad(-0.25).contains(at)) {
      move((mm) => mm.panTo(at));              // only near the edge, or a quarter screen off: no jitter
    }
  };
  const fail = (e) => {
    clear();
    setMode("off");
    toast(e.code === 1 ? "Location is turned off for this site." : "Couldn't find your location.");
  };
  const watch = (follow) => {
    stop();
    st.current.watch = navigator.geolocation.watchPosition(fix, fail,
      { enableHighAccuracy: follow, maximumAge: follow ? 5000 : 30000, timeout: 30000 });
  };

  const tap = () => {
    if (!navigator.geolocation) { toast("This browser can't share its location."); return; }
    if (mode === "off") { st.current.first = true; setMode("finding"); watch(followPref()); }
    else if (mode === "on") {
      saveFollow(true); setMode("follow"); watch(true);
      if (st.current.last) move((m) => m.panTo(st.current.last));
    } else if (mode === "follow") { saveFollow(false); setMode("on"); watch(false); }
  };

  // Panning or zooming by hand turns following off.
  useEffect(() => {
    const m = map.current;
    if (!ready || !m) return;
    const byHand = () => { if (!st.current.auto && st.current.mode === "follow") { setMode("on"); watch(false); } };
    m.on("dragstart", byHand).on("zoomstart", byHand);
    return () => m.off("dragstart", byHand).off("zoomstart", byHand);
  }, [ready]);
  useEffect(() => {
    const vis = () => {
      const s = st.current;
      if (document.hidden && s.watch != null) { stop(); s.hidden = true; }
      else if (!document.hidden && s.hidden) { s.hidden = false; if (s.mode !== "off") watch(s.mode === "follow"); }
    };
    document.addEventListener("visibilitychange", vis);
    return () => { document.removeEventListener("visibilitychange", vis); stop(); };
  }, []);
  return { mode, tap };
}

// ---- full screen ------------------------------------------------------------------------------------------------
// The browser's own where there is one; iPhone Safari has none for elements, so the map covers the page instead
// ("pseudo": fixed, the page's scroll locked, the app's header and tabs hidden). The button, Esc or ✕ leaves it.
export function useFullscreen(wrap, map) {
  const [full, setFull] = useState(false);     // false | "native" | "pseudo"
  useEffect(() => {
    const changed = () => {
      const on = (document.fullscreenElement || document.webkitFullscreenElement) === wrap.current;
      setFull((f) => (on ? "native" : f === "native" ? false : f));
    };
    document.addEventListener("fullscreenchange", changed);
    document.addEventListener("webkitfullscreenchange", changed);
    return () => { document.removeEventListener("fullscreenchange", changed); document.removeEventListener("webkitfullscreenchange", changed); };
  }, []);
  useEffect(() => {
    document.documentElement.classList.toggle("wxfull", full === "pseudo");
    const esc = (e) => { if (e.key === "Escape" && full === "pseudo") setFull(false); };
    window.addEventListener("keydown", esc);
    const t = setTimeout(() => map.current?.invalidateSize(), 50);     // the radar canvas follows the map's resize
    return () => { window.removeEventListener("keydown", esc); clearTimeout(t); };
  }, [full]);
  useEffect(() => () => document.documentElement.classList.remove("wxfull"), []);
  const toggle = () => {
    const w = wrap.current;
    if (full === "native") (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    else if (full === "pseudo") setFull(false);
    else if (w.requestFullscreen) w.requestFullscreen().catch(() => setFull("pseudo"));
    else if (w.webkitRequestFullscreen) w.webkitRequestFullscreen();
    else setFull("pseudo");
  };
  return { full, toggle };
}

// ---- ⌖ and ⛶ on any map (#80) -------------------------------------------------------------------------------------
// The buttons, my location and full screen together, for the Weather, Photos and APRS maps. `wrap` is what goes full
// screen (class "mapbox", with the map's .mapwrap inside); the map's own `ready` adds the buttons once it exists.
export function useMapTools(map, ready, wrap) {
  const locate = useLocate(map, ready);
  const fullscreen = useFullscreen(wrap, map);
  const handlers = useRef({}), tools = useRef(null);
  handlers.current = { locate: locate.tap, full: fullscreen.toggle };
  useEffect(() => {
    const m = map.current;
    if (!ready || !m) return;
    if (!m.getPane("wxme")) m.createPane("wxme").style.zIndex = 650;     // my location: over the pins, under the popups
    tools.current = toolsControl(window.L, handlers).addTo(m);
    return () => { tools.current?.remove(); tools.current = null; };
  }, [ready]);
  useEffect(() => {
    const t = tools.current;
    if (!ready || !t?.locate) return;
    t.locate.className = "wxlocate " + locate.mode;
    t.locate.setAttribute("aria-pressed", String(locate.mode === "follow"));
    const label = { off: "Show my location", finding: "Finding your location…", on: "Follow my location", follow: "Stop following" }[locate.mode];
    t.locate.title = label;
    t.locate.setAttribute("aria-label", label);
    t.full.textContent = fullscreen.full ? "✕" : "⛶";
    t.full.title = fullscreen.full ? "Leave full screen" : "Full screen";
    t.full.setAttribute("aria-label", t.full.title);
  }, [ready, locate.mode, fullscreen.full]);
  return { locate, fullscreen };
}
// The wrapper's classes for useMapTools: "mapbox", and while full screen "full native" or "full pseudo".
export const mapboxClass = (fullscreen, extra = "") => `${extra ? extra + " " : ""}mapbox${fullscreen.full ? " full " + fullscreen.full : ""}`;

// ---- the Layers panel -------------------------------------------------------------------------------------------
const SATS = [["", "None"], ["geocolor", "Color"], ["infrared", "Infrared"]];
const BASEMAPS = [["auto", "Auto (theme)"], ["light", "Light"], ["dark", "Dark"]];
const GLM_STYLES = [["smooth", "Smooth"], ["blobs", "Blobs"], ["dots", "Dots"]];
const FOCUSABLE = "button, input, a[href]";

// What's on, for the closed button.
export const layersSummary = (l) => [l.radar && "Radar", l.storms && "Storms", l.hazards && "Warnings", l.fronts && "Fronts",
  l.glm && "Lightning", l.rings && "Strikes", l.temp && "Temp", l.wind && "Wind", l.sat && "Sat"].filter(Boolean).join(" · ") || "None";

export function LayersPanel({ layers, set, strikes, legend, notes }) {
  const [open, setOpen] = useState(false);
  const panel = useRef(null), button = useRef(null), drag = useRef(null);
  const close = () => { setOpen(false); button.current?.focus(); };
  useEffect(() => {
    if (!open) return;
    panel.current?.querySelector(FOCUSABLE)?.focus();
    // A click elsewhere closes the floating panel; the phone's sheet stays while the map above it is used.
    const outside = (e) => {
      if (!matchMedia("(max-width: 639px)").matches && !panel.current?.contains(e.target) && !button.current?.contains(e.target)) setOpen(false);
    };
    const keys = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); close(); return; }
      if (e.key !== "Tab" || !panel.current) return;
      const all = [...panel.current.querySelectorAll(FOCUSABLE)].filter((x) => !x.disabled);
      if (!all.length) return;
      if (e.shiftKey && document.activeElement === all[0]) { e.preventDefault(); all[all.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === all[all.length - 1]) { e.preventDefault(); all[0].focus(); }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", keys, true);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", keys, true); };
  }, [open]);
  // The sheet's handle: dragged down far enough, it closes.
  const handle = {
    onPointerDown: (e) => { drag.current = e.clientY; e.currentTarget.setPointerCapture(e.pointerId); },
    onPointerMove: (e) => { if (drag.current != null && panel.current) panel.current.style.transform = `translateY(${Math.max(0, e.clientY - drag.current)}px)`; },
    onPointerUp: (e) => {
      const dy = drag.current == null ? 0 : e.clientY - drag.current;
      drag.current = null;
      if (panel.current) panel.current.style.transform = "";
      if (dy > 60) close();
    },
  };
  const check = (key, label, disabled = false, cls = "wxlayer") => html`<label class=${cls}><input type="checkbox" checked=${layers[key]}
    disabled=${disabled} onChange=${(e) => set({ [key]: e.target.checked })} /><span>${label}</span></label>`;
  const slider = (key, label, disabled) => html`<label class="wxslider"><span class="sr-only">${label}</span>
    <input type="range" min="0.2" max="1" step="0.05" value=${layers[key]} disabled=${disabled} aria-label=${label}
      onInput=${(e) => set({ [key]: Number(e.target.value) })} /><output>${Math.round(layers[key] * 100)}%</output></label>`;
  return html`
    <button ref=${button} type="button" class="wxlayersbtn" aria-expanded=${open} aria-controls="wxlayers" onClick=${() => (open ? close() : setOpen(true))}>
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 2 8l10 5 10-5-10-5Zm-10 9 10 5 10-5M2 16l10 5 10-5" /></svg>
      <span>Layers</span><small>${layersSummary(layers)}</small></button>
    ${open && html`<div ref=${panel} id="wxlayers" class="wxlayers" role="dialog" aria-label="Map layers">
      <div class="wxhandle" ...${handle} aria-hidden="true"><i></i></div>
      <div class="wxlayers-body">
        <h3>Layers <button type="button" class="wxclose" aria-label="Close layers" onClick=${close}>✕</button></h3>
        <div class="wxrow">${check("radar", "Radar")}${slider("radarOpacity", "Radar opacity", !layers.radar)}</div>
        ${check("storms", "Storm tracks")}
        ${check("hazards", "Watches & warnings")}
        ${check("hazardsMinor", "with advisories & marine", !layers.hazards, "wxlayer wxsub")}
        ${check("fronts", "Fronts")}
        ${check("glm", "Lightning")}
        <span class="wxradios wxsub" role="radiogroup" aria-label="Lightning style">${GLM_STYLES.map(([k, l]) => html`<label key=${k}>
          <input type="radio" name="wxglm" checked=${layers.lightningStyle === k} disabled=${!layers.glm} onChange=${() => set({ lightningStyle: k })} />
          <span>${l}</span></label>`)}</span>
        ${check("rings", `Station strikes${strikes ? ` (${strikes})` : ""}`)}
        <h3>Surface</h3>
        <div class="wxrow">${check("temp", "Temperature")}${slider("tempOpacity", "Temperature opacity", !layers.temp)}</div>
        ${check("wind", "Surface wind")}
        <h3>Satellite</h3>
        <div class="wxrow"><span class="wxradios" role="radiogroup" aria-label="Satellite">${SATS.map(([k, l]) => html`<label key=${k}>
          <input type="radio" name="wxsat" checked=${layers.sat === k} onChange=${() => set({ sat: k })} /><span>${l}</span></label>`)}</span>
          ${slider("satOpacity", "Satellite opacity", !layers.sat)}</div>
        <h3>Basemap</h3>
        <span class="wxradios" role="radiogroup" aria-label="Basemap">${BASEMAPS.map(([k, l]) => html`<label key=${k}>
          <input type="radio" name="wxbase" checked=${layers.basemap === k} onChange=${() => set({ basemap: k })} /><span>${l}</span></label>`)}</span>
        ${legend}
        <p class="muted wxnotes">${notes}</p>
      </div></div>`}`;
}

// ---- the timeline bar -------------------------------------------------------------------------------------------
export const SPEEDS = [["½×", 1200], ["1×", 600], ["2×", 300]];
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });

// timeline: the frame times; at: the index shown (-1: none); live: showing now; loaded(k): frame k ready to show;
// gap: seconds between the loop's end and now that the satellite hasn't published yet (#74); detail: the small line.
export function LoopBar({ timeline, at, live, time, detail, playing, speed, loaded, gap, onPlay, onStep, onSeek, onNow, onSpeed }) {
  const [more, setMore] = useState(false);
  const n = timeline.length, can = n > 1;
  const at15 = (k) => k > 0 && Math.floor(timeline[k] / 900) !== Math.floor(timeline[k - 1] / 900);
  const span = n ? timeline[n - 1] - timeline[0] : 0;
  const gapShare = can && gap > 600 && span ? Math.min(40, (gap / (span + gap)) * 100) : 0;
  return html`<div class=${"wxbar" + (more ? " more" : "")} aria-label="Timeline">
    <button type="button" class="extra" disabled=${!can} aria-label="Step back" onClick=${() => onStep(-1)}>◀◀</button>
    <button type="button" class="wxplay" disabled=${!can} aria-label=${playing ? "Pause" : "Play"} onClick=${onPlay}>${playing ? "❚❚" : "▶"}</button>
    <button type="button" class="extra" disabled=${!can} aria-label="Step forward" onClick=${() => onStep(1)}>▶▶</button>
    <div class="wxscrub">
      <div class="wxtrack">
        ${timeline.map((t, k) => html`<i key=${t} class=${"wxbuf" + (loaded(k) ? " in" : "")} style=${`left: ${(k / n) * 100}%; width: ${100 / n}%`}></i>`)}
        ${timeline.map((t, k) => at15(k) && html`<b key=${"t" + t} class="wxtick" style=${`left: ${((k + 0.5) / n) * 100}%`}><span>${clock(t)}</span></b>`)}
        <input type="range" aria-label="Frame" min="0" max=${Math.max(0, n - 1)} value=${Math.max(0, at)} disabled=${!can}
          aria-valuetext=${time ? clock(time) : "no frames"} onInput=${(e) => onSeek(Number(e.target.value))} />
      </div>
      ${gapShare > 0 && html`<div class="wxgap" style=${`flex-basis: ${gapShare}%`}
        title=${`The satellite's newest image is ${Math.round(gap / 60)} minutes old, so the loop ends there`}><span>sat. delay</span></div>`}
    </div>
    <div class="wxtime"><b>${time ? clock(time) : "–"}</b><small>${detail}</small></div>
    <button type="button" class="extra wxspeed" aria-label="Speed" onClick=${onSpeed}>${SPEEDS[speed][0]}</button>
    <button type="button" class=${"extra wxnow" + (live ? " on" : "")} aria-pressed=${live} onClick=${onNow}>Now</button>
    <button type="button" class="wxmore" aria-label="More controls" aria-expanded=${more} onClick=${() => setMore(!more)}>⋯</button>
  </div>`;
}
