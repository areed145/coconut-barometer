"""The web frontend: every module parses, the page fits the Content-Security-Policy,
nothing renders raw HTML, and everything the page loads exists."""
import glob
import os
import re
import subprocess
import tempfile
import unittest

STATIC = os.path.join(os.path.dirname(__file__), "..", "flickr_organizer", "web", "static")
JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
OURS = sorted(glob.glob(os.path.join(STATIC, "js", "*.js")))


class FrontendTest(unittest.TestCase):
    @unittest.skipUnless(os.path.exists(JSC), "needs macOS JavaScriptCore (jsc)")
    def test_every_module_parses(self):
        files = OURS + sorted(glob.glob(os.path.join(STATIC, "vendor", "*.js")))
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
        self.assertIn('<div class="rose-grid">${shown.map', weather)
        self.assertIn(".rose-grid { display: flex; flex-wrap: wrap;", css)
        self.assertIn(".rose-grid > .card { flex: 1 1 300px;", css)
        self.assertIn(".rose svg { flex: 1 1 180px; min-width: 0;", css)

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


if __name__ == "__main__":
    unittest.main()
