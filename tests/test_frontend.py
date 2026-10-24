"""The web frontend: every module parses, the page fits the Content-Security-Policy,
nothing renders raw HTML, and everything the page loads exists."""
import glob
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
        self.assertIn('!["home", "weather", "aprs", "status", "settings", "login"].includes(tab)', main)   # not a Photos page
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
        refs = re.findall(r'(?:src|href)="/static/([^"]+)"', open(os.path.join(STATIC, "index.html")).read())
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
