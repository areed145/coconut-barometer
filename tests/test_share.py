"""Share links: /p/<id> and /a/<id> carry link-preview tags for public photos and albums only, plus
robots.txt and the sitemap."""
import html
import os
import re
import tempfile
import unittest

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from coconut_barometer.config import from_dict
from coconut_barometer.store import Store
from coconut_barometer.sync import sync
from coconut_barometer.web.app import create_app


def meta(page: str) -> dict:
    return {k: html.unescape(v) for k, v in re.findall(r'<meta (?:property|name)="([^"]+)" content="([^"]*)">', page)}


class ShareTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        self.store = Store(path)
        sync(FakeFlickr(), self.store, log=lambda *a: None)
        x = self.store.db.execute
        x("UPDATE photos SET description='<b>Tonkotsu</b> &amp; a <a href=\"x\">bowl</a>' WHERE id='1'")
        x("UPDATE photos SET ispublic=0, title='Secret' WHERE id='2'")
        x("INSERT INTO memberships (album_id, photo_id) VALUES ('100', '1')")           # Japan
        self.store.db.commit()
        cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]}})
        self.c = TestClient(create_app(cfg, FakeFlickr(), provider_factory=lambda: FakeVision("m1"), scheduler=False),
                            base_url="https://testserver")

    def test_a_public_photo_gets_a_preview(self):
        r = self.c.get("/p/1")
        self.assertEqual(r.status_code, 200)
        m = meta(r.text)
        self.assertEqual(m["og:title"], "Ramen in Tokyo")
        self.assertEqual(m["og:description"], "Tonkotsu & a bowl")            # plain text (escaped in the HTML)
        self.assertEqual(m["og:url"], "https://testserver/p/1")
        self.assertEqual(m["og:image"], "https://live.staticflickr.com/1/1_s_b.jpg")
        self.assertEqual(m["twitter:card"], "summary_large_image")
        self.store.db.execute("UPDATE photos SET width=900, height=600 WHERE id='1'")   # no 1024 px size
        self.store.db.commit()
        self.assertEqual(meta(self.c.get("/p/1").text)["og:image"], "https://live.staticflickr.com/1/1_s_c.jpg")
        self.assertIn("<title>Ramen in Tokyo · Coconut Barometer</title>", r.text)
        self.assertIn('src="/static/js/main.js"', r.text)                     # still the app

    def test_private_or_missing_get_the_plain_page(self):
        plain = self.c.get("/p/999").text
        self.assertEqual(self.c.get("/p/2").text, plain)                      # private: same as not there
        self.assertNotIn("Secret", plain)
        self.assertEqual(meta(plain)["og:title"], "Coconut Barometer")
        self.assertEqual(self.c.get("/p/abc").status_code, 200)

    def test_titles_are_escaped(self):
        self.store.db.execute("UPDATE photos SET title='5 < 6 \"><b' WHERE id='1'")
        self.store.db.commit()
        page = self.c.get("/p/1").text
        self.assertNotIn('6 "><b', page)
        self.assertIn('content="5 &lt; 6 &quot;&gt;&lt;b"', page)

    def test_an_album_gets_a_preview(self):
        m = meta(self.c.get("/a/100").text)
        self.assertEqual((m["og:title"], m["og:url"], m["og:description"]), ("Japan", "https://testserver/a/100", "1 photo"))
        self.assertEqual(meta(self.c.get("/a/101").text)["og:title"], "Coconut Barometer")   # only a private photo in it

    def test_every_page_has_the_site_preview(self):
        m = meta(self.c.get("/").text)
        self.assertEqual(m["og:image"], "https://testserver/static/img/logo-320.png")
        self.assertEqual(m["twitter:card"], "summary")

    def test_robots_and_sitemap(self):
        self.assertIn("Sitemap: https://testserver/sitemap.xml", self.c.get("/robots.txt").text)
        r = self.c.get("/sitemap.xml")
        self.assertEqual(r.headers["content-type"], "application/xml")
        locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
        self.assertIn("https://testserver/p/1", locs)
        self.assertNotIn("https://testserver/p/2", locs)                      # private
        self.assertTrue(any("/a/" in u for u in locs))


if __name__ == "__main__":
    unittest.main()
