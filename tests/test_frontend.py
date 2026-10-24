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
        self.assertIn('WEATHER_TABS = [["live", "Live"], ["history", "History"], ["records", "Records"], ["map", "Map"]];', main)   # Stations is in Settings
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
        self.assertIn('api("/api/wx/fronts")', m)
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
          store.set("cb-wxmap-layers", "{not json");
          console.log(JSON.stringify({ first: JSON.stringify(first) === JSON.stringify(DEFAULT_LAYERS), again, bad,
            broken: loadLayers().radar, summary: layersSummary(again) }));
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
        self.assertEqual(out["summary"], "Storms · Lightning · Sat")

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
        self.assertIn('<a href="#/visitors">Visitors</a>', open(os.path.join(STATIC, "js", "home.js")).read())

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


if __name__ == "__main__":
    unittest.main()
