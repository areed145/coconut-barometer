"""The review tool's web side: the page and its files are served, the API needs the
token and the right Host, and the page's script at least parses."""
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from fakes import FakeFlickr
from flickr_organizer.review import ReviewApp
from flickr_organizer.review.server import make_server
from flickr_organizer.store import Store
from flickr_organizer.sync import sync

STATIC = os.path.join(os.path.dirname(__file__), "..", "flickr_organizer", "review", "static")


class ServerTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), "db.sqlite")
        ready = queue.Queue()

        def run():   # a sqlite connection belongs to its thread, so build everything in the server's
            store = Store(path)
            sync(FakeFlickr(), store, log=lambda *a: None)
            store.queue_put("1", "pending", [])
            server, token = make_server(ReviewApp(store, FakeFlickr(), background=False), port=0)
            ready.put((server, token))
            server.serve_forever()
        threading.Thread(target=run, daemon=True).start()
        self.server, self.token = ready.get(timeout=10)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path, **headers):
        req = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers.get("Content-Type"), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, None, ""

    def test_page_and_files_are_served(self):
        status, ctype, html = self.get("/")
        self.assertEqual((status, ctype.split(";")[0]), (200, "text/html"))
        self.assertIn(f'const TOKEN = "{self.token}"', html)     # the token is only in the page
        self.assertEqual(self.get("/static/app.js")[:2][0], 200)
        self.assertTrue(self.get("/static/style.css")[1].startswith("text/css"))
        self.assertEqual(self.get("/static/../server.py")[0], 404)

    def test_api_needs_the_token_and_the_right_host(self):
        self.assertEqual(self.get("/api/summary")[0], 403)
        status, _, body = self.get("/api/summary", **{"X-Token": self.token})
        self.assertEqual((status, json.loads(body)["pending"]), (200, 1))
        self.assertEqual(self.get("/", Host="evil.example")[0], 403)


@unittest.skipUnless(shutil.which("osascript"), "needs macOS JavaScriptCore (osascript)")
class PageScriptTest(unittest.TestCase):
    def test_app_js_parses(self):
        path = os.path.abspath(os.path.join(STATIC, "app.js"))
        js = (f"ObjC.import('Foundation'); var s = $.NSString.stringWithContentsOfFileEncodingError("
              f"'{path}', $.NSUTF8StringEncoding, null).js; try {{ new Function(s); 'ok' }} catch (e) {{ String(e) }}")
        out = subprocess.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "ok", out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
