"""Convention tags (date, place, camera, album) and EXIF fetching."""
import json
import os
import tempfile
import unittest
from unittest import mock

from fakes import photo, FakeFlickr, fake_geocoder, CFG
from coconut_barometer.camera import fetch_cameras, pending_cameras
from coconut_barometer.config import from_dict
from coconut_barometer.planner import build_plan
from coconut_barometer.store import Store
from coconut_barometer.sync import sync


class StandardTagsTest(unittest.TestCase):
    def setUp(self):
        import coconut_barometer.standard_tags as std_mod
        orig, std_mod.country_names = std_mod.country_names, lambda cc: {"US": ["United States"], "JP": ["Japan"]}[cc]
        self.addCleanup(setattr, std_mod, "country_names", orig)
        self.tmp = tempfile.mkdtemp()
        on = {"enabled": True}
        self.cfg = from_dict({**CFG, "vision": {**CFG["vision"], "enabled": False}, "standard_tags": {
            "date": on,
            "place": {"enabled": True, "extra": {"Texas": ["southwest"]}},
            "camera": {"enabled": True, "rules": [{"match": "iphone", "tags": ["mobile"]}],
                       "albums": [{"match": "^nikon", "album": "Landscapes"}]}}})
        self.store = Store(os.path.join(self.tmp, "db.duckdb"))
        self.fl = FakeFlickr()
        self.fl.photos[0]["datetaken"] = "2019-07-17 19:03:25"
        self.fl.photos[2]["tags"] = "2011 summer"                  # already dated, older style
        self.fl.photos[3]["datetakenunknown"] = "1"
        sync(self.fl, self.store, log=lambda *a: None)
        self.stats = fetch_cameras(self.fl, self.store, pending_cameras(self.store), log=lambda *a: None)

    def plan(self):
        return build_plan(self.cfg, self.store, None, geocoder=fake_geocoder)

    def tags(self):
        return {r.photo_id: r.new for r in self.plan() if r.action == "add_tags"}

    def test_camera_fetch_is_cached(self):
        self.assertEqual(self.stats, {"ok": 2, "no_camera": 3, "failed": 0})
        self.assertEqual(pending_cameras(self.store), [])     # permission-denied is cached too
        self.assertEqual(self.store.cameras()["2"], "Apple iPhone X")
        row = self.store.db.execute("SELECT lens, exif FROM cameras WHERE photo_id='1'").fetchone()
        self.assertEqual(row["lens"], "28-300mm")
        self.assertEqual(json.loads(row["exif"])["ExposureTime"], "1/250 sec")   # readable form preferred

    def test_all_groups_in_priority_order(self):
        t = self.tags()
        self.assertEqual(t["1"], ["y2019", "m07", "d17", "japan", "asia", "tokyo", "nikon", "d90"])
        # Austin: configured city name, state extras.
        self.assertEqual(t["2"], ["y2024", "m01", "d01", "unitedstates", "americas", "northamerica", "texas",
                                  "tx", "austin", "southwest", "apple", "iphone", "x", "mobile"])
        self.assertIn("y2024", t["3"])                       # an older-style year tag doesn't stop the date tags
        self.assertNotIn("houston", t["3"])                  # accuracy 6: no city tag
        self.assertNotIn("4", t)                             # no geo, date unknown, no camera: nothing

    def test_camera_albums_and_cap(self):
        albums = {(r.photo_id, r.new) for r in self.plan() if r.action == "add_to_album"}
        self.assertIn(("1", "Landscapes"), albums)
        with mock.patch("coconut_barometer.rules.base.MAX_TAGS_PER_PHOTO", 4):
            t = self.tags()
        self.assertEqual(t["1"], ["y2019", "m07", "d17", "japan"])
        self.assertEqual(t["3"], ["y2024", "m01"])               # its 2 existing tags leave room for 2

    def test_disabled_by_default(self):
        self.cfg = from_dict({**CFG, "vision": {**CFG["vision"], "enabled": False}})
        self.assertEqual(self.tags(), {})


if __name__ == "__main__":
    unittest.main()
