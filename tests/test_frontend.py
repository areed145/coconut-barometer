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
        self.assertIn('aria-label="Map layers"', m)                         # radar and storm tracks toggle apart
        self.assertIn(">Storm tracks</button>", m)
        self.assertIn(">Fronts</button>", m)                                # WPC's fronts and highs and lows (#69)
        self.assertIn('api("/api/wx/fronts")', m)
        self.assertNotIn("Radar loop", m)                                   # no loop mode: one timeline
        self.assertIn("loopWindow(", m)                                     # one timeline for them all (wx_loop.js)
        self.assertIn('fetch("/api/wx/map/errors"', m)
        self.assertIn('window.addEventListener("unhandledrejection"', m)

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

    @unittest.skipUnless(NODE, "needs Node")
    def test_radar_colors_are_the_settings_stops_everywhere(self):
        """#75: one function for the levels, the storm cells and the legend; a stop's own color is what shows."""
        script = r"""
          import { radarColor, levelColor, radarScale, DEFAULT_STOPS as S } from "%s";
          const stops = [{ dbz: 15, color: "#000000" }, { dbz: 25, color: "#ffffff" }, { dbz: 25, color: "#ff0000" }, { dbz: 45, color: "#0000ff" }];
          console.log(JSON.stringify({
            exact: [radarColor(S, 15), radarColor(S, 35)], between: radarColor(stops, 20), clamp: [radarColor(S, 0), radarColor(S, 99)],
            dup: radarColor(stops, 25), dupBetween: radarColor(stops, 35),
            level: [levelColor(S, 15), levelColor(S, 19.9), levelColor(S, 58), levelColor(S, 55), levelColor(S, null)],
            legend: radarScale(S), firstLevels: [15, 20, 25].map((d) => levelColor(S, d)),
          }));
        """ % os.path.join(STATIC, "js", "radar_layer.js")
        out = json.loads(subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60).stdout)
        self.assertEqual(out["exact"], ["#8fd3ff", "#f2d22e"])                 # a stop: exactly its color
        self.assertEqual(out["between"], "rgb(128,128,128)")                   # halfway between black and white
        self.assertEqual(out["clamp"], ["#8fd3ff", "#b03ab5"])
        self.assertNotIn("NaN", out["dup"] + out["dupBetween"])                # two stops at one dBZ: no NaN
        self.assertEqual(out["level"][:2], ["#8fd3ff", "#8fd3ff"])            # the 15-20 level is the 15 stop's color
        self.assertEqual(out["level"][2], out["level"][3])                     # a 58 dBZ cell: its level's (55) color
        self.assertIsNone(out["level"][4])                                     # no dBZ: no color (drawn neutral)
        for c in out["firstLevels"]:                                           # the legend: the levels' own colors
            self.assertIn(c, out["legend"])

    def test_analytics_views_are_drawn_in_the_color_gradient(self):
        """#68: the views lines use Settings -> Display -> Colors; bars and the dashed previous period stay flat."""
        js = lambda name: open(os.path.join(STATIC, "js", name)).read()
        self.assertIn("s.gradient ? (u) => upward(u)", js("chart.js"))
        self.assertIn('color: "--chart-1", gradient: true }];', js("analytics.js"))
        self.assertIn('color: "--chart-1", gradient: true }]', js("analytics_tags.js"))
        self.assertNotIn("gradient", js("analytics_more.js"))                  # favers: bars, flat
        self.assertIn('window.dispatchEvent(new Event("cb:gradient"))', js("lib.js"))   # a changed gradient redraws them
        self.assertIn('window.addEventListener("cb:gradient", on)', js("lib.js"))

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

    def test_home_is_the_front_page(self):
        lib = open(os.path.join(STATIC, "js", "lib.js")).read()
        main = open(os.path.join(STATIC, "js", "main.js")).read()
        home = open(os.path.join(STATIC, "js", "home.js")).read()
        self.assertIn('const parts = (path || "home").split("/");', lib)          # an empty address is Home
        self.assertIn('<a class="brand" href="#/" aria-label="Coconut Barometer: Home">', main)
        self.assertIn('if (tab === "home") page = html`<${Home}', main)
        self.assertIn('!["home", "weather", "aprs", "status", "settings", "login", "alerts"].includes(tab)', main)   # not a Photos page
        for href in ('"#/photos"', '"#/weather"', '"#/aprs"'):
            self.assertIn(f"href={href}", home)
        self.assertIn('${session && html`<nav class="homelinks"', home)           # private pages: signed in only
        self.assertNotRegex(home, r"\.(lat|lon)\b")                              # no station or beacon positions

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
