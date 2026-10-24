"""The site's own visitors (#83): what's counted, what's left out, that no IP is kept, and the report."""
import datetime as dt
import json
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from coconut_barometer.config import from_dict
from coconut_barometer.geoip import GeoIP, _months
from coconut_barometer.store import Store
from coconut_barometer.sync import sync
from coconut_barometer.visitors import (PER_IP_HOUR, PER_VISITOR_DAY, SESSION_GAP, Visitors, normalize, parse_ua,
                                        ref_host)
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import hash_password

PASSWORD = "correct horse battery"
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/18.0 Mobile/15E148 Safari/604.1")
PIXEL = ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 "
         "Mobile Safari/537.36")
EDGE = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 "
        "Safari/537.36 Edg/130.0.0.0")
DAY = 86400
NOON = dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc).timestamp()


class FakeGeo:
    updating, error = False, None

    def lookup(self, ip):
        return {"city": "Bakersfield", "region": "California", "country": "United States", "country_code": "US"}

    def age(self, now=None):
        return 3 * DAY

    def due(self, now=None):
        return False


def visitors(**cfg):
    return Visitors({"visitors": {"enabled": True, "keep_days": 90, "honor_gpc": True, **cfg}}, "unused", FakeGeo())


class CollectorTest(unittest.TestCase):
    def test_routes_become_templates_and_items(self):
        self.assertEqual(normalize("#/"), ("/home", None))
        self.assertEqual(normalize("#/photos/photo/53120"), ("/photos/photo/:id", "53120"))
        self.assertEqual(normalize("#/photos/photostream?album=7215&tag=x"), ("/photos/photostream", "album:7215"))
        self.assertEqual(normalize("#/photos/map/@35.3,-119.0,9"), ("/photos/map", None))
        self.assertEqual(normalize("#/weather/history?range=7d"), ("/weather/history", None))
        self.assertEqual(normalize("/p/42"), ("/p/:id", "42"))
        self.assertEqual(normalize("#/<script>/x"), ("/home", None))       # anything odd ends it

    def test_only_the_parsed_user_agent_is_kept(self):
        self.assertEqual(parse_ua(IPHONE), {"device": "phone", "browser": "Safari", "os": "iOS"})
        self.assertEqual(parse_ua(PIXEL), {"device": "phone", "browser": "Chrome", "os": "Android"})
        self.assertEqual(parse_ua(EDGE), {"device": "desktop", "browser": "Edge", "os": "Windows"})
        self.assertEqual(parse_ua(IPHONE.replace("iPhone; CPU iPhone", "iPad; CPU"))["device"], "tablet")

    def test_referrer_is_just_another_sites_host(self):
        self.assertEqual(ref_host("https://www.google.com/search?q=coconut", "cb.example"), "google.com")
        self.assertIsNone(ref_host("https://cb.example/#/weather", "cb.example"))
        self.assertIsNone(ref_host("android-app://com.slack", "cb.example"))
        self.assertIsNone(ref_host("", "cb.example"))

    def test_a_visitor_lasts_a_day_and_visits_end_after_a_pause(self):
        v = visitors()
        for t in (NOON, NOON + 60, NOON + 60 + SESSION_GAP + 1, NOON + DAY):
            self.assertTrue(v.hit("page", "#/", "203.0.113.5", IPHONE, now=t))
        rows = v.buffer
        self.assertEqual(rows[0][2], rows[1][2])                 # same person, same day
        self.assertEqual(rows[0][3], rows[1][3])                 # one visit
        self.assertEqual(rows[1][2], rows[2][2])
        self.assertNotEqual(rows[1][3], rows[2][3])              # a pause starts another
        self.assertNotEqual(rows[2][2], rows[3][2])              # next day: no way to tell it's them
        v = visitors()
        v.hit("page", "#/", "203.0.113.5", IPHONE, now=NOON)
        v.hit("page", "#/", "203.0.113.5", PIXEL, now=NOON)
        self.assertNotEqual(v.buffer[0][2], v.buffer[1][2])      # same address, another device

    def test_no_ip_or_user_agent_is_kept(self):
        v = visitors()
        v.hit("page", "#/photos", "203.0.113.5", IPHONE, referrer="https://example.org/x", now=NOON)
        flat = " ".join(str(x) for x in v.buffer[0])
        self.assertNotIn("203.0.113", flat)
        self.assertNotIn("Mozilla", flat)
        self.assertIn("Bakersfield", flat)

    def test_bots_are_kept_apart_and_named(self):
        v = visitors()
        v.hit("page", "#/", "198.51.100.1", "Mozilla/5.0 (compatible; Googlebot/2.1)", now=NOON)
        v.hit("page", "#/", "198.51.100.1", "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/130.0.0.0 Safari/537.36", now=NOON)
        v.hit("page", "#/", "198.51.100.1", "python-requests/2.32", now=NOON)
        v.hit("page", "#/", "198.51.100.1", "", now=NOON)
        self.assertEqual([(r[4], r[16]) for r in v.buffer], [("bot", "Googlebot"), ("bot", "HeadlessChrome"),
                                                             ("bot", "python-requests"), ("bot", "no user agent")])

    def test_limits_per_address_and_per_visitor(self):
        v = visitors()
        sent = sum(v.hit("page", "#/", "203.0.113.9", f"ua{i % 2}", now=NOON + i) for i in range(PER_IP_HOUR + 5))
        self.assertEqual(sent, PER_IP_HOUR)
        self.assertEqual(v.left_out["rate"], 5)
        v = visitors()
        for i in range(PER_VISITOR_DAY + 3):
            v._per_ip.clear()                                    # under the hourly limit, over the daily one
            v.hit("page", "#/", "203.0.113.9", IPHONE, now=NOON + i)
        self.assertEqual((len(v.buffer), v.left_out["limit"]), (PER_VISITOR_DAY, 3))

    def test_off_counts_nothing(self):
        v = visitors(enabled=False)
        self.assertFalse(v.hit("page", "#/", "203.0.113.5", IPHONE, now=NOON))
        self.assertEqual(v.buffer, [])

    def test_geoip_without_a_file_finds_nothing_and_tries_last_month(self):
        g = GeoIP(tempfile.mkdtemp())
        self.assertEqual(g.lookup("8.8.8.8"), {})
        self.assertTrue(g.due())
        self.assertEqual(_months(dt.date(2026, 1, 3)), ["2026-01", "2025-12"])

        class NotOut:
            status_code = 404
            def __enter__(self): return self
            def __exit__(self, *a): return False
        asked = []
        self.assertFalse(g.refresh(get=lambda url, **kw: asked.append(url) or NotOut(), today=dt.date(2026, 1, 3)))
        self.assertEqual([u.rsplit("-", 2)[-2:] for u in asked], [["2026", "01.mmdb.gz"], ["2025", "12.mmdb.gz"]])
        self.assertFalse(g.due())                                # waits before trying again
        self.assertIsNotNone(g.error)


class VisitorsWebTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        self.store = Store(path)
        sync(FakeFlickr(), self.store, log=lambda *a: None)
        self.store.set_password_hash("owner", hash_password(PASSWORD))
        self.cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]}})
        self.app = create_app(self.cfg, FakeFlickr(), provider_factory=lambda: FakeVision("m"), scheduler=False)
        self.rt = self.app.state.runtime
        self.rt.visitors.geo = FakeGeo()
        self.c = TestClient(self.app, base_url="https://testserver")

    def visitor(self):
        return TestClient(self.app, base_url="https://testserver")

    def send(self, client, body=None, **headers):
        r = client.post("/api/seen", content=body if isinstance(body, str) else json.dumps(
            body or {"p": "#/weather/live", "w": 390, "r": "https://www.google.com/", "u": {"source": "qr"}}),
            headers={"user-agent": IPHONE, "content-type": "text/plain", **headers})
        self.assertEqual(r.status_code, 204)

    def rows(self):
        self.rt.visitors.flush(self.store)
        return [dict(r) for r in self.store.db.execute("SELECT * FROM site_hits ORDER BY ts")]

    def login(self):
        return self.c.post("/api/auth/login", json={"username": "owner", "password": PASSWORD})

    def test_a_visitors_page_view_is_stored(self):
        self.send(self.visitor())
        [row] = self.rows()
        self.assertEqual((row["kind"], row["route"], row["ref_host"], row["utm_source"], row["city"], row["device"], row["width"]),
                         ("page", "/weather/live", "google.com", "qr", "Bakersfield", "phone", 390))

    def test_the_owner_isnt_counted_signed_in_or_out(self):
        self.login()
        self.send(self.c)
        self.c.post("/api/auth/logout", headers={"X-CSRF-Token": self.c.get("/api/auth/session").json()["csrf"]})
        self.assertNotIn("fo_session", self.c.cookies)
        self.assertIn("cb_owner", self.c.cookies)                # stays after signing out
        self.send(self.c)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rt.visitors.left_out["owner"], 2)

    def test_privacy_signals_other_sites_and_junk_are_left_out(self):
        v = self.visitor()
        self.send(v, **{"sec-gpc": "1"})
        self.send(v, **{"dnt": "1"})
        self.send(v, origin="https://evil.example")
        self.send(v, **{"sec-fetch-site": "cross-site"})
        self.send(v, body="not json")
        self.send(v, body={"p": 5})
        self.send(v, body={"p": "#/" + "x" * 3000})
        self.assertEqual(self.rows(), [])
        self.cfg["visitors"]["honor_gpc"] = False
        self.send(v, **{"sec-gpc": "1", "origin": "https://testserver", "sec-fetch-site": "same-origin"})
        self.assertEqual(len(self.rows()), 1)

    def test_link_previews_of_share_pages_are_counted(self):
        v = self.visitor()
        v.get("/p/1", headers={"user-agent": "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)"})
        v.get("/p/1", headers={"user-agent": IPHONE})          # a person: their page's beacon counts them
        v.get("/p/999", headers={"user-agent": "Slackbot-LinkExpanding 1.0"})   # no such photo
        self.assertEqual([(r["kind"], r["route"], r["item"], r["browser"]) for r in self.rows()],
                         [("preview", "/p/:id", "1", "Slack")])

    def test_old_days_keep_their_totals(self):
        old = (dt.date.today() - dt.timedelta(days=120)).isoformat()
        cols = "ts, day, visitor, session, kind, route"
        x = self.store.db.execute
        for visitor in ("a", "a", "b"):
            x(f"INSERT INTO site_hits ({cols}) VALUES (1, ?, ?, 's', 'page', '/home')", (old, visitor))
        x(f"INSERT INTO site_hits ({cols}) VALUES (1, ?, 'z', 's', 'bot', '/home')", (old,))
        self.rt.visitors.maintain(self.store)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM site_hits").fetchone()[0], 0)
        self.assertEqual(self.store.visitors_series("2000-01-01"), [{"day": old, "visitors": 2, "views": 3}])

    def test_the_report_needs_the_login(self):
        self.assertEqual(self.c.get("/api/analytics/visitors").status_code, 401)
        self.send(self.visitor())
        self.send(self.visitor(), body={"p": "#/photos/photo/1"})
        self.login()
        d = self.c.get("/api/analytics/visitors", params={"days": 7}).json()
        self.assertEqual((d["totals"]["visitors"], d["totals"]["views"], d["totals"]["sessions"], d["totals"]["now"]), (1, 2, 1, 1))
        self.assertEqual([r["photo_id"] for r in d["photos"]], ["1"])
        self.assertEqual(d["cities"][0]["label"], "Bakersfield, California")
        self.assertEqual(d["regions"][0]["label"], "California, United States")
        self.assertEqual(d["series"][-1]["views"], 2)
        self.assertIn("visitors", [r["id"] for r in self.c.get("/api/status").json()["rows"]])

    def test_homes_glance_needs_the_login(self):
        self.assertEqual(self.c.get("/api/visitors/glance").status_code, 401)
        self.send(self.visitor())
        self.send(self.visitor(), body={"p": "#/weather"})
        self.login()
        d = self.c.get("/api/visitors/glance").json()
        self.assertEqual((d["enabled"], d["visitors"], d["views"], d["now"], d["city"]), (True, 1, 2, 1, "Bakersfield, California"))
        self.assertEqual(len(d["days"]), 14)
        self.assertEqual(d["days"][-1], 1)                                       # today, last


if __name__ == "__main__":
    unittest.main()
