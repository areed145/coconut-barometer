"""The Map page's API: geotagged points (filtered, compressed), cards, and its settings."""
import gzip
import json
import os
import re
import tempfile
import unittest

from fastapi.testclient import TestClient

from fakes import FakeFlickr, CFG
from coconut_barometer.config import DEFAULTS, _merge, from_dict, validate
from coconut_barometer.store import Store
from coconut_barometer.sync import sync
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import CSP, hash_password

PASSWORD = "correct horse battery"
STATIC = os.path.join(os.path.dirname(__file__), "..", "coconut_barometer", "web", "static")


class MapTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        self.store = Store(path)
        self.fl = FakeFlickr()
        self.fl.photos[4]["datetaken"] = "2019-06-01 10:00:00"
        sync(self.fl, self.store, log=lambda *a: None)
        self.store.set_password_hash("owner", hash_password(PASSWORD))
        self.cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]},
                              "map": {"color_high": "#00ff00"}})
        self.c = TestClient(create_app(self.cfg, self.fl, scheduler=False), base_url="https://testserver")
        self.h = {"X-CSRF-Token": self.c.post("/api/auth/login", json={"username": "owner", "password": PASSWORD}).json()["csrf"]}

    def points(self, **params):
        r = self.c.get("/api/map/points", params=params)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_points_are_the_geotagged_photos(self):
        geo = {p["id"] for p in self.fl.photos if float(p["latitude"] or 0) or float(p["longitude"] or 0)}
        d = self.points()
        self.assertEqual({p[0] for p in d["points"]}, geo)
        self.assertNotIn("4", geo)                                       # no geotag: not on the map
        self.assertEqual(d["points"][0][3:], ["1", "s"])                 # server, secret for the thumbnail
        self.assertEqual((d["colors"], d["expand_at"]), (["#f7d125", "#8abe35", "#00ff00"], 40))
        self.assertEqual(d["years"], [2019, 2024])

    def test_filters_by_year_and_album(self):
        self.assertEqual([p[0] for p in self.points(year_to=2019)["points"]], ["5"])
        self.assertNotIn("5", [p[0] for p in self.points(year_from=2020)["points"]])
        memberships = self.store.memberships()
        album = next(a for ids in memberships.values() for a in ids)
        inside = {pid for pid, ids in memberships.items() if album in ids}
        self.assertTrue({p[0] for p in self.points(album=album)["points"]} <= inside)

    def test_compressed_when_asked(self):
        r = self.c.get("/api/map/points", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.headers.get("content-encoding"), "gzip")
        self.assertIn("points", r.json())                                # the client decompresses it
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_visitors_see_public_photos_only(self):
        self.store.db.execute("UPDATE photos SET ispublic=0 WHERE id='5'")
        self.store.db.commit()
        anon = TestClient(self.c.app, base_url="https://testserver")
        r = anon.get("/api/map/points")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("5", {p[0] for p in r.json()["points"]})
        self.assertIn("5", {p[0] for p in self.points()["points"]})       # signed in: all of them

    def test_cards_in_the_order_asked(self):
        r = self.c.post("/api/map/cards", json={"ids": ["3", "nope", "1"]}, headers=self.h)
        self.assertEqual([c["id"] for c in r.json()], ["3", "1"])
        self.assertIn("q", r.json()[0]["sizes"])
        self.assertEqual(self.c.post("/api/map/cards", json={"ids": [str(i) for i in range(501)]},
                                     headers=self.h).status_code, 422)

    def test_config_carries_the_carto_key(self):
        self.assertEqual(self.c.get("/api/map/config").json()["carto_key"], "")
        self.c.app.state.runtime.cfg["map"]["carto_key"] = "abc123-key_XYZ"
        self.assertEqual(self.c.get("/api/map/config").json(),
                         {"carto_key": "abc123-key_XYZ", "colors": ["#f7d125", "#8abe35", "#00ff00"], "expand_at": 40})
        raw = {"map": {"carto_key": "bad key with spaces"}}
        self.assertTrue(any("map.carto_key" in e for e in validate(raw, _merge(DEFAULTS, raw))[0]))

    def test_colour_settings_are_checked(self):
        raw = {"theme": {"color_low": "yellow"}, "map": {"expand_at": 1}}
        errors, _ = validate(raw, _merge(DEFAULTS, raw))
        self.assertTrue(any("theme.color_low: should be a color" in e for e in errors))
        self.assertTrue(any("map.expand_at" in e for e in errors))

    def test_radar_color_stops_each_need_their_own_dbz(self):
        same = [{"dbz": 20, "color": "#000000"}, {"dbz": 20, "color": "#ffffff"}]
        raw = {"theme": {"radar_colors": same}}
        self.assertIn("theme.radar_colors: two stops at the same dBZ; give each its own", validate(raw, _merge(DEFAULTS, raw))[0])
        raw = {"theme": {"radar_colors": [same[0], {"dbz": 30, "color": "#ffffff"}]}}
        self.assertEqual([e for e in validate(raw, _merge(DEFAULTS, raw))[0] if "radar_colors" in e], [])

    def test_tiles_are_allowed_and_the_libraries_are_here(self):
        self.assertIn("https://basemaps.cartocdn.com ", CSP)
        loader = open(os.path.join(STATIC, "js", "leaflet.js")).read()
        for ref in re.findall(r'"/static/(vendor/[^"]+)"', loader):
            self.assertTrue(os.path.exists(os.path.join(STATIC, ref)), ref)
        self.assertIn("basemaps.cartocdn.com", loader)


if __name__ == "__main__":
    unittest.main()
