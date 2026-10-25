"""The web frontend: every module parses, the page fits the Content-Security-Policy,
nothing renders raw HTML, and everything the page loads exists."""
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

STATIC = os.path.join(os.path.dirname(__file__), "..", "coconut_barometer", "web", "static")
JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
NODE = shutil.which("node")      # elsewhere (CI): Node checks module syntax instead
OURS = sorted(glob.glob(os.path.join(STATIC, "js", "*.js")))


class FrontendTest(unittest.TestCase):
    @unittest.skipUnless(os.path.exists(JSC) or NODE, "needs macOS JavaScriptCore (jsc) or Node")
    def test_every_module_parses(self):
        files = OURS + sorted(glob.glob(os.path.join(STATIC, "vendor", "*.js")))
        if not os.path.exists(JSC):
            for f in files:
                with open(f) as src:
                    r = subprocess.run([NODE, "--input-type=module", "--check"], stdin=src, capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, f"{f}: {r.stderr}")
            return
        script = os.path.join(tempfile.mkdtemp(), "check.js")
        with open(script, "w") as f:
            f.write("for (const f of arguments) { try { checkModuleSyntax(read(f)); print('ok ' + f); } "
                    "catch (e) { print('ERROR ' + f + ': ' + e); } }")
        out = subprocess.run([JSC, script, "--", *files], capture_output=True, text=True, timeout=60).stdout
        self.assertEqual(out.count("ok "), len(files), out)

    def test_page_fits_the_csp(self):
        page = open(os.path.join(STATIC, "index.html")).read()
        self.assertNotRegex(page, r"<script(?![^>]*\bsrc=)[^>]*>")        # no inline scripts
        self.assertNotIn("<style", page)
        self.assertNotRegex(page, r"\son\w+=")                            # no inline handlers
        self.assertNotRegex(page, r'\sstyle="')

    def test_nothing_renders_raw_html(self):
        for path in OURS:
            src = open(path).read()
            for risky in ("innerHTML", "outerHTML", "dangerouslySetInnerHTML", "insertAdjacentHTML", "document.write", "eval("):
                self.assertNotIn(risky, src, f"{os.path.basename(path)} uses {risky}")

    def test_wind_roses_wrap_and_scale(self):
        weather = open(os.path.join(STATIC, "js", "weather.js")).read()
        css = open(os.path.join(STATIC, "css", "app.css")).read()
        self.assertIn('<div class="wxgrid">${body}</div>', weather)
        self.assertIn("<h2>Wind rose${suffix(id)}</h2>", weather)
        self.assertIn(".wxgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%,", css)
        self.assertIn(".rose svg { flex: 1 1 180px; min-width: 0;", css)

    def test_weather_stream_status_is_on_cards_and_in_settings(self):
        weather = open(os.path.join(STATIC, "js", "weather.js")).read()
        admin = open(os.path.join(STATIC, "js", "admin.js")).read()
        css = open(os.path.join(STATIC, "css", "app.css")).read()
        self.assertIn('class=${"wxstatus-dot " + (connected ? "s-applied" : "s-failed")}', weather)
        self.assertIn('stream=${d.stream}', weather)
        self.assertNotIn("StreamBadge", weather)
        self.assertIn('export function WxStations()', weather)
        main = open(os.path.join(STATIC, "js", "main.js")).read()
        self.assertIn('WEATHER_TABS = [["live", "Live"], ["history", "History"], ["records", "Records"], ["map", "Map"], ["qc", "QC"]];', main)   # Stations is in Settings
        self.assertIn('section === "weather" && html`<${WxStations} />`', admin)
        self.assertIn(".wxstatus-dot {", css)

    def test_aprs_sparks_and_tracks_use_metric_gradients_and_refresh(self):
        aprs = open(os.path.join(STATIC, "js", "aprs.js")).read()
        weather = open(os.path.join(STATIC, "js", "weather.js")).read()
        self.assertIn('values=${t.speeds} lo=${0}', aprs)
        self.assertIn('values=${t.alts}', aprs)
        self.assertEqual(aprs.count("gradient />"), 2)
        self.assertIn('value="speed">Speed</option><option value="altitude">Altitude</option>', aprs)
        self.assertIn("shade(hi > lo ? (v - lo) / (hi - lo) : 0.5)", aprs)
        self.assertIn("const stop = every(load, 15000);", aprs)
        self.assertIn("gradient = false", weather)
        self.assertIn("gradient ? shade(share)", weather)

    def test_map_crowds_at_one_spot_open_without_endless_zooming(self):
        m = open(os.path.join(STATIC, "js", "map.js")).read()
        self.assertIn("else if (apart < RADIUS || m.getZoom() >= max)", m)   # never splits: open its photos now
        self.assertIn("else c.zoomToBounds({ padding: [30, 30] });", m)      # else straight to where it splits
        self.assertNotIn("m.getZoom() + 1", m)

    def test_weather_map_layers_share_a_timeline_and_report_its_errors(self):
        m = open(os.path.join(STATIC, "js", "weather_map.js")).read()
        c = open(os.path.join(STATIC, "js", "wx_controls.js")).read()
        self.assertIn('role="dialog" aria-label="Map layers"', c)           # radar and storm tracks toggle apart
        self.assertIn('check("storms", "Storm tracks")', c)
        self.assertIn('check("fronts", "Fronts")', c)                       # WPC's fronts and highs and lows (#69)
        self.assertIn('useFeed(showFronts, "/api/wx/fronts", 15 * 60000)', m)
        self.assertIn('check("hazards", "Watches & warnings")', c)          # the NWS's watches and warnings (#87)
        self.assertIn('check("hazardsMinor", "with advisories & marine", !layers.hazards', c)
        self.assertIn('useFeed(showHazards, view && `/api/wx/hazards?${radarQuery(view)}`, 120000)', m)
        self.assertIn('createPane("wxhazards").style.zIndex = 355', m)      # under the radar: echoes show through
        self.assertIn("if (x.pending) soon = setTimeout(load, PENDING_MS);", m)
        self.assertIn('check("temp", "Temperature")', c)                    # RTMA's temperature and wind (#85, #86)
        self.assertIn('slider("tempOpacity", "Temperature opacity", !layers.temp)', c)
        self.assertIn('check("wind", "Surface wind")', c)
        self.assertIn('createPane("wxtemp").style.zIndex = 345', m)
        self.assertIn('createPane("wxwind").style.zIndex = 365', m)
        self.assertIn("useFeed(showTemp, surfaceQuery && `/api/wx/temperature?${surfaceQuery}`, 5 * 60000)", m)
        self.assertIn("inside(v, map.current, SURFACE_ZOOMS) ? v : viewFor(map.current, SURFACE_ZOOMS)", m)
        self.assertNotIn("Radar loop", m)                                   # no loop mode: one timeline
        self.assertIn("loopWindow(", m)                                     # one timeline for them all (wx_loop.js)
        self.assertIn('fetch("/api/wx/map/errors"', m)
        self.assertIn('window.addEventListener("unhandledrejection"', m)

    def test_weather_map_controls_sit_on_the_map(self):
        """#78: my location and full screen with the zoom buttons, a Layers panel or sheet, a timeline bar."""
        m = open(os.path.join(STATIC, "js", "weather_map.js")).read()
        c = open(os.path.join(STATIC, "js", "wx_controls.js")).read()
        css = open(os.path.join(STATIC, "css", "app.css")).read()
        self.assertNotIn("wxmapbar", m)                                     # no toolbar above the map
        self.assertIn('<${LayersPanel} layers=${layers}', m)
        self.assertIn('<${LoopBar} timeline=${timeline}', m)
        self.assertIn('createPane("wxme").style.zIndex = 650', c)           # my location: over the radar and pins, under popups
        self.assertIn("useMapTools(map, ready, wrap)", m)
        # where I am: asked only on a tap, watched with less accuracy unless following, stopped while hidden
        self.assertIn("navigator.geolocation.watchPosition(fix, fail,", c)
        self.assertIn("{ enableHighAccuracy: follow, maximumAge: follow ? 5000 : 30000", c)
        self.assertIn('m.on("dragstart", byHand).on("zoomstart", byHand)', c)   # by hand: following stops
        self.assertIn("!m.getBounds().pad(-0.25).contains(at)", c)          # recentred only near the edge
        self.assertIn('document.hidden && s.watch != null', c)
        self.assertNotIn("api(", c)                                         # the position never goes to the server
        # full screen: the browser's, else the page covered (iPhone), and the map told its new size
        self.assertIn("w.requestFullscreen", c)
        self.assertIn('else setFull("pseudo");', c)
        self.assertIn("map.current?.invalidateSize()", c)
        self.assertIn(".mapbox.pseudo { position: fixed; inset: 0;", css)
        self.assertIn("html.wxfull header.top, html.wxfull .subtabs { display: none; }", css)
        # layers: one button with what's on, its panel a bottom sheet on a phone, the choices remembered
        self.assertIn("aria-expanded=${open}", c)
        self.assertIn("localStorage.setItem(PREFS, JSON.stringify(layers))", c)
        self.assertIn("useEffect(() => saveLayers(layers), [layers]);", m)
        self.assertIn('slider("radarOpacity", "Radar opacity"', c)          # an opacity per raster layer
        self.assertIn('slider("satOpacity", "Satellite opacity"', c)
        self.assertIn(".wxlayers { top: auto; left: 0; right: 0; bottom: 0; width: auto; max-height: 50%;", css)
        # the timeline: step, play, speed, Now, keyboard, and paused while hidden
        self.assertIn('export const SPEEDS = [["½×", 1200], ["1×", 600], ["2×", 300]];', c)
        self.assertIn("SPEEDS[speed][1]", m)
        self.assertIn('e.key === "ArrowLeft" || e.key === "ArrowRight"', m)
        self.assertIn("if (document.hidden) setPlaying(false);", m)
        self.assertIn('class="wxgap"', c)                                  # the satellite's delay, shaded (#74)

    @unittest.skipUnless(NODE, "needs Node")
    def test_layer_choices_survive_a_reload(self):
        script = r"""
          const store = new Map();
          globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) };
          const { loadLayers, saveLayers, DEFAULT_LAYERS, layersSummary } = await import("%s");
          const first = loadLayers();
          saveLayers({ ...first, radar: false, sat: "infrared", satOpacity: 0.5, basemap: "dark" });
          const again = loadLayers();
          store.set("cb-wxmap-layers", '{"radar": "yes", "bogus": 1}');      // a bad value: the default
          const bad = loadLayers();
          store.set("cb-wxmap-layers", '{"lightning": false}');             // the rings' old name (#94)
          const old = loadLayers();
          store.set("cb-wxmap-layers", '{"glmStyle": "dots"}');             // the style's old name (#101): a choice kept ...
          const dots = loadLayers().lightningStyle;
          store.set("cb-wxmap-layers", '{"glmStyle": "blobs"}');            // ... the old default not
          const blobs = loadLayers().lightningStyle;
          store.set("cb-wxmap-layers", "{not json");
          console.log(JSON.stringify({ first: JSON.stringify(first) === JSON.stringify(DEFAULT_LAYERS), again, bad, old, dots, blobs,
            broken: loadLayers().radar, summary: layersSummary(again), first_hazards: first.hazards, first_minor: first.hazardsMinor }));
        """ % os.path.join(STATIC, "js", "wx_controls.js")
        stub = os.path.join(tempfile.mkdtemp(), "run.mjs")
        with open(stub, "w") as f:      # wx_controls.js imports lib.js (Preact): enough of a DOM for it to load
            f.write("globalThis.document = { documentElement: { getAttribute: () => null }, addEventListener() {} };"
                    "globalThis.window = { addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) };"
                    "globalThis.matchMedia = window.matchMedia; globalThis.location = { hash: '' };\n" + script)
        r = subprocess.run([NODE, stub], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(out["first"])
        self.assertEqual({k: out["again"][k] for k in ("radar", "sat", "satOpacity", "basemap", "storms")},
                         {"radar": False, "sat": "infrared", "satOpacity": 0.5, "basemap": "dark", "storms": True})
        self.assertEqual((out["bad"]["radar"], "bogus" in out["bad"], out["broken"]), (True, False, True))
        self.assertEqual(out["summary"], "Storms · Warnings · Lightning · Strikes · Sat")
        self.assertEqual((out["old"]["rings"], out["old"]["glm"], "lightning" in out["old"]), (False, True, False))
        self.assertEqual((out["first"] and out["again"]["lightningStyle"], out["dots"], out["blobs"]), ("smooth", "dots", "smooth"))
        self.assertEqual((out["first_hazards"], out["first_minor"]), (True, False))   # warnings on, advisories off

    @unittest.skipUnless(NODE, "needs Node")
    def test_a_tap_finds_the_watches_and_warnings_there(self):
        """#87: alerts decoded as radar shapes are; a tap inside finds them most important first, not in a hole,
        and the minor ones only when they're shown."""
        script = r"""
          import { decodeHazards, alertsAt } from "%s";
          // whole 1/100 degrees: a square 37-38 N, 106-105 W with a hole 37.4-37.6 N, 105.6-105.4 W; and a warning inside it
          const sq = (w, s, e, n) => [w, s, e - w, 0, 0, n - s, w - e, 0];
          const poly = (w, s, e, n, ...holes) => [w, s, e, n, sq(w, s, e, n), ...holes.map((h) => sq(...h))];
          const x = { shapes: { level: "fine", q: 100, alerts: [
            { id: "tor", event: "Tornado Warning", color: "#FF0000", minor: false, polys: [poly(-10580, 3770, -10560, 3790)] },
            { id: "watch", event: "Flood Watch", color: "#2E8B57", minor: false, polys: [poly(-10600, 3700, -10500, 3800, [-10560, 3740, -10540, 3760])] },
            { id: "sca", event: "Small Craft Advisory", color: "#D8BFD8", minor: true, polys: [poly(-10600, 3700, -10500, 3800)] } ] } };
          const d = decodeHazards(x), ids = (lat, lon, minor) => alertsAt(d.alerts, lat, lon, minor).map((a) => a.id);
          console.log(JSON.stringify({ level: d.level, both: ids(37.8, -105.7, false), hole: ids(37.5, -105.5, false),
            minor: ids(37.2, -105.2, true), outside: ids(36, -105.5, true), none: decodeHazards(null) }));
        """ % os.path.join(STATIC, "js", "hazards_layer.js")
        stub = os.path.join(tempfile.mkdtemp(), "run.mjs")
        with open(stub, "w") as f:
            f.write(script)
        r = subprocess.run([NODE, stub], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out, {"level": "fine", "both": ["tor", "watch"], "hole": [], "minor": ["watch", "sca"], "outside": [], "none": None})

    @unittest.skipUnless(NODE, "needs Node")
    def test_surface_grids_read_back_and_wind_arrows_agree_with_the_pins(self):
        """#85, #86: a grid decoded from int16 tenths and read bilinearly (NaN off it and beside a missing point);
        a wind arrow points the way the wind blows, as a station pin's arrow does (rotated wind_dir + 180)."""
        script = r"""
          import { decodeGrid, sample, arrow, TEMP_STOPS } from "%s";
          import { rampColor } from "%s";
          const ints = new Int16Array([100, 200, 300, 400, 500, -32768]);         // 2 rows of 3: 10, 20, 30 / 40, 50, -
          const b64 = Buffer.from(ints.buffer).toString("base64");
          const g = decodeGrid({ valid: 1, lat0: 30, lon0: -100, dlat: 1, dlon: 1, nx: 3, ny: 2, scale: 0.1, missing: -32768, fields: { t: b64 } });
          const at = (lat, lon) => { const v = sample(g, "t", lat, lon); return Number.isNaN(v) ? null : Math.round(v * 100) / 100; };
          // A recording canvas: the first stroke's moveTo (tail) and lineTo (tip).
          const bearing = (u, v) => {
            const pts = [];
            const ctx = new Proxy({}, { get: (_, k) => (k === "moveTo" || k === "lineTo" ? (x, y) => pts.push([x, y]) : () => {}), set: () => true });
            arrow(ctx, 0, 0, u, v, 40, "#fff");
            const [[x0, y0], [x1, y1]] = pts;
            return Math.round(((Math.atan2(x1 - x0, -(y1 - y0)) * 180) / Math.PI + 360) %% 360);
          };
          const from = [0, 90, 225, 300], pins = from.map((d) => (d + 180) %% 360);
          const arrows = from.map((d) => { const r = (d * Math.PI) / 180; return bearing(-5 * Math.sin(r), -5 * Math.cos(r)); });
          console.log(JSON.stringify({ corner: at(30, -100), mid: at(30, -99.5), off: at(29, -100), missing: at(30.5, -98.5),
            none: decodeGrid(null), pins, arrows, freezing: rampColor(TEMP_STOPS, 0) }));
        """ % (os.path.join(STATIC, "js", "surface_layer.js"), os.path.join(STATIC, "js", "ramp.js"))
        stub = os.path.join(tempfile.mkdtemp(), "run.mjs")
        with open(stub, "w") as f:
            f.write(script)
        r = subprocess.run([NODE, stub], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual((out["corner"], out["mid"], out["off"], out["missing"], out["none"]), (10, 15, None, None, None))
        self.assertEqual(out["arrows"], out["pins"])                        # the way it blows, as the pins show it
        self.assertEqual(out["freezing"], "#4fc3f7")                        # 32 °F is a stop of its own

    @unittest.skipUnless(NODE, "needs Node")
    def test_the_loop_is_the_hour_both_radar_and_satellite_cover(self):
        """#74: radar kept from ~2¼ h ago up to now, satellite ending 40 min ago (GIBS is late)."""
        script = r"""
          import { loopWindow, LOOP } from "%s";
          const now = 1_800_000_000, every = (from, to, step) => { const a = []; for (let t = from; t <= to; t += step) a.push(t); return a; };
          const radar = every(now - 134 * 60, now, 120), sat = every(now - 130 * 60, now - 40 * 60, 600);
          const latest = (a, t) => a.filter((x) => x <= t).pop();
          const both = loopWindow(radar, sat);
          const alone = loopWindow(radar, []);
          const filling = loopWindow(every(now - 20 * 60, now, 120), sat);       // radar history just after a deploy
          console.log(JSON.stringify({
            both: [both.end, both.start, both.times[0], both.times.at(-1), both.times.every((t) => latest(radar, t) && latest(sat, t))],
            alone: [alone.times[0], alone.times.at(-1), alone.times.length === alone.radar.length],
            satOnly: loopWindow([], sat).times.length === sat.length,
            filling: [filling.end, filling.times[0]], now, LOOP,
          }));
        """ % os.path.join(STATIC, "js", "wx_loop.js")
        out = json.loads(subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60).stdout)
        now, hour = out["now"], out["LOOP"]
        end = now - 40 * 60
        self.assertEqual(out["both"], [end, end - hour, end - hour, end, True])   # the hour ending at the newest satellite frame
        self.assertEqual(out["alone"][1] - out["alone"][0] <= hour and out["alone"][1], now)    # radar only: its last hour
        self.assertTrue(out["alone"][2] and out["satOnly"])
        self.assertEqual(out["filling"][0], now)                               # no radar in that hour yet: the radar's own

    @unittest.skipUnless(NODE, "needs Node")
    def test_lightning_merges_only_whats_new(self):
        """#94: each answer's minutes replace the ones held, and what's older than wanted is dropped."""
        script = r"""
          import { mergeMinutes, ageStops, TRAIL } from "%s";
          const AGE_STOPS = ageStops(null);
          const held = new Map([[60, [1, 2, 1]], [120, [1, 2, 1]], [180, [3, 4, 1]]]);
          const merged = mergeMinutes(held, { minutes: [[180, [3, 4, 2]], [240, [5, 6, 1]], [0, [9, 9, 9]]] }, 120);
          console.log(JSON.stringify({ merged: [...merged], none: [...mergeMinutes(held, null, 0)].length,
            ends: [AGE_STOPS[0].at, AGE_STOPS.at(-1).at], TRAIL }));
        """ % os.path.join(STATIC, "js", "lightning_layer.js")
        out = json.loads(subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60).stdout)
        self.assertEqual(out["merged"], [[120, [1, 2, 1]], [180, [3, 4, 2]], [240, [5, 6, 1]]])
        self.assertEqual(out["none"], 3)
        self.assertEqual(out["ends"], [0, out["TRAIL"]])                       # the colors span the trail shown

    def test_the_weather_maps_names_sit_over_its_layers(self):
        """#95: the Weather map's base without names, the names as tiles of their own over the satellite and under the
        radar (and light on dark over the satellite), the borders under them, the other maps as before."""
        js = lambda name: open(os.path.join(STATIC, "js", name)).read()
        lf, m = js("leaflet.js"), js("weather_map.js")
        self.assertIn('url(labels ? "nolabels" : "all")', lf)
        self.assertIn('L.tileLayer(url("only_labels"), { pane: "cblabels"', lf)
        self.assertIn("baseMap(lf, box.current, { minZoom: 3, labels: LABELS_Z })", m)
        self.assertIn('m.setLabels(sat ? "dark" : null);', m)
        self.assertNotIn("labels:", js("map.js") + js("aprs.js") + js("igate.js"))
        panes = {k: int(v) for k, v in re.findall(r'createPane\("(\w+)"\)\.style\.zIndex = (\d+)', m)}
        names = int(re.search(r"const LABELS_Z = (\d+);", m).group(1))
        self.assertLess(max(panes["wxsat"], panes["wxhazards"], panes["wxlines"]), names)   # over the satellite and borders ...
        self.assertLess(names, panes["wxradar"])                           # ... under the radar

    @unittest.skipUnless(NODE, "needs Node")
    def test_borders_over_the_satellite_decode(self):
        """#95: Natural Earth's lines, delta-encoded, decode to where they are."""
        path = os.path.join(STATIC, "vendor", "natural-earth-lines.json")
        data = json.load(open(path))
        self.assertEqual(set(data["layers"]), {"coast", "lake", "country", "state"})
        self.assertTrue(all(len(line) >= 4 and len(line) % 2 == 0 for lines in data["layers"].values() for line in lines))
        script = r"""
          import { decodeLine } from "%s";
          import { readFileSync } from "node:fs";
          const d = JSON.parse(readFileSync("%s", "utf8"));
          const all = Object.values(d.layers).flat().map((enc) => decodeLine(enc, d.q));
          const box = all.reduce((b, l) => [Math.min(b[0], l.bounds[0]), Math.min(b[1], l.bounds[1]), Math.max(b[2], l.bounds[2]),
            Math.max(b[3], l.bounds[3])], [Infinity, Infinity, -Infinity, -Infinity]);
          // Galveston Island's coast is somewhere near 29.3 N, 94.8 W.
          const near = all.some((l) => l.pts.some(([lat, lon]) => Math.abs(lat - 29.3) < 0.2 && Math.abs(lon + 94.8) < 0.2));
          console.log(JSON.stringify({ line: decodeLine([-9500, 2900, 10, -5, -20, 0], 100).pts, box, near }));
        """ % (os.path.join(STATIC, "js", "lines_layer.js"), path)
        out = json.loads(subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60).stdout)
        self.assertEqual(out["line"], [[29, -95], [28.95, -94.9], [28.95, -95.1]])
        self.assertLess(out["box"][0], -150)                               # the Americas, GOES-East's side ...
        self.assertTrue(out["near"])                                       # ... and the Texas coast where it is

    @unittest.skipUnless(NODE, "needs Node")
    def test_lightning_blobs_keep_every_flash_and_the_newest_on_top(self):
        """A lone flash still makes a (small) blob; crowded flashes make an opaque core; the newer band covers the
        older; the default colors are config.py's."""
        from coconut_barometer.config import DEFAULTS
        script = r"""
          import { paintBlobs, peak, radiusFor, DEFAULT_STOPS } from "%s";
          const nx = 60, ny = 30, img = () => ({ data: new Uint8ClampedArray(nx * ny * 4) });
          const px = (im, x, y) => [...im.data.slice((y * nx + x) * 4, (y * nx + x) * 4 + 4)];
          const lone = img(); paintBlobs(lone, nx, ny, [[[255, 0, 0], [[10, 10, 1]]]], 2);
          const lit = (im) => { let n = 0; for (let i = 3; i < im.data.length; i += 4) n += im.data[i] > 0; return n; };
          const crowd = img(); paintBlobs(crowd, nx, ny, [[[255, 0, 0], [[30, 15, 20]]]], 2);
          const both = img(); paintBlobs(both, nx, ny, [[[255, 0, 0], [[30, 15, 20]]], [[0, 0, 255], [[30, 15, 20]]]], 2);
          console.log(JSON.stringify({ lone: lit(lone), loneCentre: px(lone, 10, 10), far: px(lone, 40, 10),
            core: px(crowd, 30, 15), halo: px(crowd, 30 + 6, 15), top: px(both, 30, 15),
            r: [radiusFor(1), radiusFor(3), radiusFor(6)], peak: peak(2), stops: DEFAULT_STOPS }));
        """ % os.path.join(STATIC, "js", "lightning_layer.js")
        out = json.loads(subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60).stdout)
        self.assertGreater(out["lone"], 4)                                  # a lone strike: still there ...
        self.assertEqual(out["loneCentre"][:3], [255, 0, 0])                # ... in its color
        self.assertEqual(out["far"], [0, 0, 0, 0])
        self.assertGreater(out["core"][3], out["halo"][3])                  # crowded: an opaque core in a halo
        self.assertGreater(out["top"][2], 200)                              # the newer band on top ...
        self.assertLess(out["top"][0], 40)                                  # ... the older barely through
        self.assertEqual(out["r"], sorted(out["r"]))
        self.assertTrue(0 < out["peak"] < 1)
        self.assertEqual(out["stops"], DEFAULTS["theme"]["lightning_colors"])

    @unittest.skipUnless(NODE, "needs Node")
    def test_smooth_lightning_is_drawn_by_the_radars_canvas(self):
        """#101: the server's lightning shapes decode as radar's do, keeping each level's age (or core), and the radar's
        canvas colors a level by the key it's given, filled (no contour lines)."""
        script = r"""
          import { decode } from "%s";
          const sq = [-9700, 3300, -9690, 3310, [-9700, 3300, 10, 0, 0, 10, -10, 0]];
          const d = decode({ level: "fine", q: 100, bands: [{ age: 900, polys: [sq] }, { age: 112, polys: [sq] }] });
          console.log(JSON.stringify({ ages: d.bands.map((b) => b.age), rings: d.bands[0].polys[0].rings.length, none: decode({ q: 1 }) }));
        """ % os.path.join(STATIC, "js", "radar_layer.js")
        out = json.loads(subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60).stdout)
        self.assertEqual((out["ages"], out["rings"], out["none"]), ([900, 112], 1, None))
        radar = open(os.path.join(STATIC, "js", "radar_layer.js")).read()
        m = open(os.path.join(STATIC, "js", "weather_map.js")).read()
        self.assertIn("this._colorOf(band[key])", radar)
        self.assertNotIn("stroke()", radar)
        self.assertNotIn('"core"', m)
        self.assertIn('radarCanvas(lf.L, "wxglmshapes", glmLink, "age")', m)
        panes = {k: int(v) for k, v in re.findall(r'createPane\("(\w+)"\)\.style\.zIndex = (\d+)', m)}
        self.assertLess(panes["wxglmshapes"], panes["wxlightning"])          # the newest flashes over the shapes

    def test_an_alert_opens_its_own_page(self):
        main = open(os.path.join(STATIC, "js", "main.js")).read()
        self.assertIn('lazy(() => import("./alert_page.js"), "AlertPage")', main)
        self.assertIn('tab === "alerts"', main)                             # #/alerts/<at>/<key> (#73)
        page = open(os.path.join(STATIC, "js", "alert_page.js")).read()
        self.assertIn('api("/api/push/alert"', page)

    def test_photos_and_aprs_maps_have_location_and_full_screen(self):
        """#80: the Weather map's ⌖ and ⛶ on the Photos and APRS maps too, from the same hook."""
        js = lambda name: open(os.path.join(STATIC, "js", name)).read()
        for name in ("map.js", "aprs.js", "weather_map.js", "igate.js"):
            self.assertIn("useMapTools(map, ready, wrap)", js(name))
            self.assertIn("class=${mapboxClass(fullscreen", js(name))
        photos = js("map.js")                                               # the lightbox opens inside what goes full screen
        self.assertLess(photos.index("<div ref=${wrap}"), photos.index("<${Lightbox}"))
        self.assertLess(photos.index("<${Lightbox}"), photos.rindex("</div>`;"))
        css = open(os.path.join(STATIC, "css", "app.css")).read()
        self.assertIn(".mapbox .leaflet-top.leaflet-left { top: auto; left: auto; right: 0;", css)   # thumb reach on a phone

    @unittest.skipUnless(NODE, "needs Node")
    def test_radar_colors_are_the_settings_stops_everywhere(self):
        """#75: one function for the levels, the storm cells and the legend; a stop's own color is what shows."""
        script = r"""
          import { radarColor, levelColor, radarScale, DEFAULT_STOPS as S } from "%s";
          const stops = [{ dbz: 15, color: "#000000" }, { dbz: 25, color: "#ffffff" }, { dbz: 25, color: "#ff0000" }, { dbz: 45, color: "#0000ff" }];
          console.log(JSON.stringify({
            exact: [radarColor(S, 15), radarColor(S, 35)], between: radarColor(stops, 20), clamp: [radarColor(S, 0), radarColor(S, 99)],
            dup: radarColor(stops, 25), dupBetween: radarColor(stops, 35),
            level: [levelColor(S, 15), levelColor(S, 16.9), levelColor(S, 58), levelColor(S, 57), levelColor(S, null)],
            legend: radarScale(S), firstLevels: [15, 20, 25].map((d) => levelColor(S, d)),
          }));
        """ % os.path.join(STATIC, "js", "radar_layer.js")
        out = json.loads(subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60).stdout)
        self.assertEqual(out["exact"], ["#8fd3ff", "#f2d22e"])                 # a stop: exactly its color
        self.assertEqual(out["between"], "rgb(128,128,128)")                   # halfway between black and white
        self.assertEqual(out["clamp"], ["#8fd3ff", "#b03ab5"])
        self.assertNotIn("NaN", out["dup"] + out["dupBetween"])                # two stops at one dBZ: no NaN
        self.assertEqual(out["level"][:2], ["#8fd3ff", "#8fd3ff"])            # the 15-17 level is the 15 stop's color
        self.assertEqual(out["level"][2], out["level"][3])                     # a 58 dBZ cell: its level's (57) color
        self.assertIsNone(out["level"][4])                                     # no dBZ: no color (drawn neutral)
        for c in out["firstLevels"]:                                           # the legend: the levels' own colors
            self.assertIn(c, out["legend"])

    def test_analytics_views_are_drawn_in_the_color_gradient(self):
        """#68/#79: analytics lines and the audience heatmap use Settings -> Display -> Colors."""
        js = lambda name: open(os.path.join(STATIC, "js", name)).read()
        self.assertIn("s.gradient ? (u) => upward(u)", js("chart.js"))
        self.assertIn('color: "--chart-1", gradient: true }];', js("analytics.js"))
        self.assertIn('color: "--chart-1", gradient: true }]', js("analytics_tags.js"))
        viz = js("viz.js")
        self.assertIn("useThemeTick();", viz)                                  # refresh after a gradient setting changes
        self.assertIn("shade(logShare(v, max))", viz)
        self.assertIn('class="hcell"', viz)                                    # no fixed accent levels
        self.assertNotIn('class=${"hcell l"', viz)
        self.assertIn("ramp()", viz)                                            # heatmap color key
        self.assertIn('window.dispatchEvent(new Event("cb:gradient"))', js("lib.js"))   # a changed gradient redraws them
        self.assertIn('window.addEventListener("cb:gradient", on)', js("lib.js"))

    def test_audience_heatmap_fits_narrow_cards(self):
        css = open(os.path.join(STATIC, "css", "app.css")).read()
        self.assertIn("repeat(24, minmax(0, 1fr))", css)
        self.assertIn(".heat .hcol3 { visibility: hidden; }", css)            # phone labels every 6 hours, keeping their cells (#81)
        self.assertNotIn(".hcol3 { display: none", css)                       # out of the grid, every cell after shifts
        self.assertIn(".heat .hday-short { display: inline; }", css)

    def test_settings_panels_sit_with_their_settings(self):
        admin = open(os.path.join(STATIC, "js", "admin.js")).read()
        self.assertIn('section === "display" && html`<${ThemeSetting} />`', admin)
        self.assertIn('section === "connections" && html`<${FlickrPanel} />`', admin)
        self.assertIn('newPart && s.part === "Sync" && html`<${SyncPanel} />`', admin)
        self.assertIn('section === "aprs" && html`<${AprsStream} />`', admin)
        self.assertIn('api("/api/push/history")', admin)
        self.assertIn("<h2>Recent notifications</h2>", admin)
        self.assertIn('<a href=${x.url}>View chart or map</a>', admin)
        self.assertIn("String(v)", admin)
        self.assertIn('location.replace("#/settings/" + id)', admin)   # the section is in the address

    def test_visitors_is_a_page_of_its_own(self):
        """Visitors (#83) counts the whole site, so it's a signed-in page of its own, not a tab of Photos → Analytics."""
        main = open(os.path.join(STATIC, "js", "main.js")).read()
        analytics = open(os.path.join(STATIC, "js", "analytics.js")).read()
        self.assertIn('const PRIVATE_TABS = ["visitors", "status", "settings"];', main)
        self.assertIn('${session && link("visitors", "Visitors")}', main)
        self.assertIn('else if (tab === "visitors") page = html`<${Visitors} />`;', main)
        self.assertIn('location.replace("#/visitors")', main)                     # the old tab's links still work
        self.assertNotIn('"visitors"', analytics)
        home = open(os.path.join(STATIC, "js", "home.js")).read()
        self.assertIn('<a href="#/visitors">Visitors</a>', home)
        self.assertIn('<${AprsCard} />${session && html`<${VisitorsCard} />`}', home)   # Home's card: signed in only

    def test_home_is_the_front_page(self):
        lib = open(os.path.join(STATIC, "js", "lib.js")).read()
        main = open(os.path.join(STATIC, "js", "main.js")).read()
        home = open(os.path.join(STATIC, "js", "home.js")).read()
        self.assertIn('const parts = (path || "home").split("/");', lib)          # an empty address is Home
        self.assertIn('<a class="brand" href="#/" aria-label="Coconut Barometer: Home">', main)
        self.assertIn('if (tab === "home") page = html`<${Home}', main)
        self.assertIn('!["home", "weather", "aprs", "visitors", "status", "settings", "login", "alerts"].includes(tab)', main)   # not a Photos page
        for href in ('"#/photos"', '"#/weather"', '"#/aprs"'):
            self.assertIn(f"href={href}", home)
        self.assertIn('${session && html`<nav class="homelinks"', home)           # private pages: signed in only
        self.assertNotRegex(home, r"\.(lat|lon)\b")                              # no station or beacon positions
        # The Radar card (#77): after Weather, signed in only (its picture shows where the stations are), with alt text
        self.assertIn('<${WeatherCard} />${session && html`<${RadarCard} />`}<${AprsCard} />', home)
        self.assertIn('href="#/weather/map" title="Radar"', home)
        self.assertIn("/api/wx/radar/snapshot.png?theme=", home)
        self.assertIn("alt=${alt}", home)

    def test_every_module_main_imports_is_preloaded(self):
        seen, todo = set(), [os.path.join("js", "main.js")]
        while todo:
            f = todo.pop()
            if f in seen:
                continue
            seen.add(f)
            src = open(os.path.join(STATIC, f)).read()
            todo += [os.path.normpath(os.path.join(os.path.dirname(f), m))
                     for m in re.findall(r'^\s*import\s[^;]*?from\s+"(\.\.?/[^"]+)"', src, re.M)]
        page = open(os.path.join(STATIC, "index.html")).read()
        preloaded = re.findall(r'<link rel="modulepreload" href="/static/([^"]+)">', page)
        self.assertEqual(len(preloaded), len(set(preloaded)))           # no duplicates
        self.assertEqual(set(preloaded), seen - {os.path.join("js", "main.js")})

    def test_pages_load_when_opened_and_exist(self):
        main = open(os.path.join(STATIC, "js", "main.js")).read()
        pages = re.findall(r'const (\w+) = lazy\(\(\) => import\("\./([\w.]+)"\), "(\w+)"\);', main)
        self.assertGreaterEqual(len(pages), 14)
        for const, module, name in pages:
            src = open(os.path.join(STATIC, "js", module)).read()
            self.assertRegex(src, rf"export (function|const) {name}\b", f"{const}: {module} has no export {name}")
        statics = re.findall(r'^import\s[^;]*?from\s+"\./([\w.]+)"', main, re.M)
        self.assertEqual(sorted(statics), ["home.js", "lib.js"])                  # nothing else up front

    def test_everything_the_page_loads_exists(self):
        refs = re.findall(r'(?:src|href)="/static/([^"?]+)', open(os.path.join(STATIC, "index.html")).read())
        for path in OURS:
            refs += [os.path.normpath(os.path.join("js", m)) for m in re.findall(r'from\s+"(\.\.?/[^"]+)"', open(path).read())]
        self.assertTrue(refs)
        for ref in refs:
            self.assertTrue(os.path.exists(os.path.join(STATIC, ref)), ref)

    def test_send_to_review_deep_links_to_photo(self):
        with open(os.path.join(STATIC, "js", "gallery.js")) as f:
            gallery = f.read()
        with open(os.path.join(STATIC, "js", "main.js")) as f:
            main = f.read()
        with open(os.path.join(STATIC, "js", "review.js")) as f:
            review = f.read()
        self.assertIn("#/photos/review?photo_id=${encodeURIComponent(p.id)}", gallery)
        self.assertIn("targetPhotoId=${route.params.photo_id}", main)
        self.assertIn("const queue = targetPhotoId && targetAt < 0 ? [targetPhotoId, ...list] : list", review)
        self.assertIn("const at = targetPhotoId ? (targetAt < 0 ? 0 : targetAt) : 0", review)

    def test_photo_grid_infinite_scroll_rearms_after_each_page(self):
        with open(os.path.join(STATIC, "js", "gallery.js")) as f:
            gallery = f.read()
        self.assertIn("x.isIntersecting || x.intersectionRatio > 0", gallery)
        self.assertIn("}, [more, photos.length, total]);", gallery)
        self.assertIn('addEventListener("scroll", near, { passive: true });', gallery)

    def test_lightbox_preloads_neighbours_once_the_photo_is_in(self):
        gallery = open(os.path.join(STATIC, "js", "gallery.js")).read()
        self.assertIn("onLoad=${preload}", gallery)
        self.assertIn("for (const i of [index + step, index - step, index + 2 * step])", gallery)
        self.assertIn("if (frugal()) return;", gallery)                     # not on Save-Data or 2G
        self.assertIn("photoDetail(p.id).then(setDetail)", gallery)         # the info panel uses what was preloaded
        self.assertIn("img.src = pick(n.sizes, SHOWN);", gallery)           # never the zoom sizes (Flickr throttles them)

    def test_lightbox_related_strips(self):
        """#124: nearby and same-day strips; a pick browses that strip until Back; See all is the photostream filtered."""
        gallery = open(os.path.join(STATIC, "js", "gallery.js")).read()
        self.assertIn("api(`/api/photos/${id}/related`)", gallery)
        self.assertIn('photosLink({ near: r.near?.join(","), sort: "taken" })', gallery)
        self.assertIn('photosLink({ day: r.day, sort: "oldest" })', gallery)
        self.assertIn('"on", "day", "near",', gallery[gallery.index("const FILTERS"):])
        self.assertIn("onRelated=${(photos, index, label) => setAlt({ photos, index, label })}", gallery)

    def test_photostream_timeline(self):
        """#126: the date sorts get a rail; a jump lands once its page is laid out; photos above load back in with the one on
        screen kept where it was."""
        gallery = open(os.path.join(STATIC, "js", "gallery.js")).read()
        self.assertIn('const DATED = ["newest", "oldest", "taken", "taken_asc"];', gallery)
        self.assertIn('api("/api/photo-months", { params: query })', gallery)
        self.assertIn("less(before)", gallery)
        self.assertIn("if (t) scrollBy(0, t.getBoundingClientRect().top - a.top);", gallery)
        self.assertIn("useLayoutEffect(() => {                           // only once the page is laid out", gallery)
        self.assertIn('role="slider" tabindex="0"', gallery)                                  # and the arrow keys

    def test_gear_page_links_to_the_photostream(self):
        """#127: a public Photos tab; each bar or row opens the photostream filtered to it, and the chips read plainly."""
        gear = open(os.path.join(STATIC, "js", "gear.js")).read()
        main = open(os.path.join(STATIC, "js", "main.js")).read()
        gallery = open(os.path.join(STATIC, "js", "gallery.js")).read()
        self.assertIn('["gear", "Gear"]', main)
        self.assertNotIn('"gear"', main[main.index("const PRIVATE_PHOTO_TABS"):main.index("const PRIVATE_TABS")])
        self.assertIn('go({ setting: `${k}:${r.low}-${r.high ?? ""}` })', gear)
        self.assertIn('"camera", "lens", "setting",', gallery)
        if NODE:
            start = gallery.index("const shutter = ")                         # the chip's formatter, run as it is
            script = gallery[start:gallery.index("\n}\n", gallery.index("function setting(")) + 3]
            out = subprocess.run([NODE, "-e", script + 'console.log(JSON.stringify(["focal:24-28", "shutter:0.002-0.004", "iso:6400-", "f:0-1.4"].map(setting)))'],
                                 capture_output=True, text=True, timeout=60).stdout
            self.assertEqual(json.loads(out), [["Focal length", "24 mm to 28 mm"], ["Shutter", "1/500 to 1/250"],
                                               ["ISO", "6400 and over"], ["Aperture", "under f/1.4"]])

    def test_trips_page_and_trip_view(self):
        """#125: a public Trips tab of cards; a trip is the photostream filtered to it, in the order taken, with a map."""
        gallery = open(os.path.join(STATIC, "js", "gallery.js")).read()
        main = open(os.path.join(STATIC, "js", "main.js")).read()
        self.assertIn('["trips", "Trips"]', main)
        self.assertNotIn('"trips"', main[main.index("const PRIVATE_PHOTO_TABS"):main.index("const PRIVATE_TABS")])
        self.assertIn('photosLink({ trip: t.id, sort: "taken_asc" })', gallery)
        self.assertIn("<${PointsMap} points=${tripPoints} />", gallery)
        self.assertIn("<dt>Part of</dt>", gallery)

    def test_lightbox_slideshow(self):
        """#130: S or the button plays; the next photo after the chosen seconds; held on hover and in a hidden tab; stops at the end."""
        gallery = open(os.path.join(STATIC, "js", "gallery.js")).read()
        self.assertIn('else if (e.key === "s") play(!playing);', gallery)
        self.assertIn("setTimeout(() => (index < photos.length - 1 ? onIndex(index + 1) : play(false)), secs * 1000)", gallery)
        self.assertIn("if (!playing || held) return;", gallery)
        self.assertIn("const vis = () => setHeld(document.hidden);", gallery)
        self.assertIn("<${PhotoGrid} paged=${paged} slideshow=${slideshow}", gallery)
        self.assertIn("try { localStorage.setItem(PACE_KEY, n); } catch {}", gallery)     # storage may be blocked

    def test_lightbox_touch_gestures(self):
        """#129: swipe, pinch and double-tap on the photo; the mouse keeps click-to-zoom; no motion when reduced."""
        gallery = open(os.path.join(STATIC, "js", "gallery.js")).read()
        css = open(os.path.join(STATIC, "css", "app.css")).read()
        self.assertIn('<div class=${"stage" + (zoom ? " zoomed" : "")} ...${touch.handlers}>', gallery)
        self.assertIn("onClick=${() => touch.touched() || setZoom(!zoom)}", gallery)      # a tap isn't a click-to-zoom
        self.assertIn("onPointerCancel: onPointerUp", gallery)
        self.assertIn(".lightbox .stage { touch-action: none; }", css)                     # the page behind doesn't scroll
        self.assertIn("@media (prefers-reduced-motion: reduce) { .lightbox .stage img { transition: none; } }", css)

    @unittest.skipUnless(NODE, "needs Node")
    def test_justified_rows_fill_the_width(self):
        """#128: every full row is exactly the width; the last row stays at the target height; adding photos only adds rows."""
        script = r"""
          import { aspect, justify } from "%s";
          const width = 1000, gap = 6, target = 220;
          const rnd = (i) => [1.5, 0.67, 1, 1.78, 0.75, 2.4, 1.33][i %% 7];
          const a = Array.from({ length: 50 }, (_, i) => rnd(i));
          const rows = justify(a, width, target, gap);
          const widths = rows.map((r) => a.slice(r.start, r.end).reduce((s, x) => s + x * r.height, 0) + gap * (r.end - r.start - 1));
          console.log(JSON.stringify({
            rows, widths, more: justify([...a, ...a], width, target, gap).slice(0, rows.length - 1),
            covered: rows.map((r) => r.end - r.start).reduce((s, n) => s + n, 0),
            aspects: [aspect({ width: 4000, height: 3000 }), aspect({ width: 9000, height: 1000 }), aspect({ width: 100, height: 900 }),
                      aspect({ width: null, height: 10 })],
            wide: justify([5, 1, 1], 300, 220, 6),
          }));
        """ % os.path.join(STATIC, "js", "justify.js")
        out = json.loads(subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60).stdout)
        self.assertEqual(out["covered"], 50)                                    # every photo, once, in order
        for r, w in zip(out["rows"][:-1], out["widths"][:-1]):
            self.assertAlmostEqual(w, 1000, places=6)
            self.assertLess(abs(r["height"] - 220), 120)                        # near the target
        self.assertTrue(out["rows"][-1]["last"])
        self.assertEqual(out["rows"][-1]["height"], 220)
        self.assertLessEqual(out["widths"][-1], 1000)
        self.assertEqual(out["more"], out["rows"][:-1])                         # a new page doesn't move rows already shown
        self.assertEqual(out["aspects"], [4000 / 3000, 3, 0.5, 1])              # panoramas and strips limited; unknown: square
        self.assertEqual(out["wide"][0], {"start": 0, "end": 1, "height": 60})  # one photo wider than the row: a row of its own


if __name__ == "__main__":
    unittest.main()
