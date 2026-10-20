"""Convention tags (date, place, camera, album, coords, owner, filler) and EXIF fetching."""
import json
import os
import tempfile
import unittest

from fakes import photo, FakeFlickr, fake_geocoder, CFG
from flickr_organizer.camera import fetch_cameras, pending_cameras
from flickr_organizer.config import from_dict
from flickr_organizer.planner import build_plan
from flickr_organizer.store import Store
from flickr_organizer.sync import sync


class StandardTagsTest(unittest.TestCase):
    def setUp(self):
        import flickr_organizer.standard_tags as std_mod
        orig, std_mod.country_names = std_mod.country_names, lambda cc: {"US": ["United States"], "JP": ["Japan"]}[cc]
        self.addCleanup(setattr, std_mod, "country_names", orig)
        self.tmp = tempfile.mkdtemp()
        on = {"enabled": True}
        self.cfg = from_dict({**CFG, "vision": {**CFG["vision"], "enabled": False}, "standard_tags": {
            "date": on, "coords": on, "album_tags": {"enabled": True, "albums": {"Texas": ["lonestar"]}},
            "place": {"enabled": True, "extra": {"Texas": ["southwest"]}},
            "camera": {"enabled": True, "rules": [{"match": "iphone", "tags": ["mobile"]}],
                       "albums": [{"match": "^nikon", "album": "Landscapes"}]},
            "owner": {"enabled": True, "tags": ["me"]}, "filler": {"enabled": True, "tags": ["photo"]}}})
        self.store = Store(os.path.join(self.tmp, "db.sqlite"))
        self.fl = FakeFlickr()
        self.fl.photos[0]["datetaken"] = "2019-07-17 19:03:25"
        self.fl.photos[2]["tags"] = "2011 summer"                  # already dated, older style
        self.fl.photos[3]["datetakenunknown"] = "1"
        sync(self.fl, self.store, log=lambda *a: None)
        self.stats = fetch_cameras(self.fl, self.store, pending_cameras(self.store), log=lambda *a: None)

    def plan(self):
        return build_plan(self.cfg, self.store, None, geocoder=fake_geocoder)

    def tags(self):
        return {r.photo_id: r.target.split(" | ") for r in self.plan() if r.action == "add_tags"}

    def test_camera_fetch_is_cached(self):
        self.assertEqual(self.stats, {"ok": 2, "no_camera": 3, "failed": 0})
        self.assertEqual(pending_cameras(self.store), [])     # permission-denied is cached too
        self.assertEqual(self.store.cameras()["2"], "Apple iPhone X")
        row = self.store.db.execute("SELECT lens, exif FROM cameras WHERE photo_id='1'").fetchone()
        self.assertEqual(row["lens"], "28-300mm")
        self.assertEqual(json.loads(row["exif"])["ExposureTime"], "1/250 sec")   # readable form preferred

    def test_all_groups_in_priority_order(self):
        t = self.tags()
        self.assertEqual(t["1"], ["y2019", "m07", "d17", "japan", "asia", "tokyo", "nikon", "d90",
                                  "lat360", "lon1400", "me", "photo"])
        # Austin: configured city name, Texas album tag (already a member), state extras.
        self.assertEqual(t["2"], ["y2024", "m01", "d01", "unitedstates", "americas", "northamerica", "texas",
                                  "tx", "austin", "southwest", "apple", "iphone", "x", "mobile", "lonestar",
                                  "lat300", "lon980", "me", "photo"])
        self.assertNotIn("y2011", t["3"])                    # skip_if_dated
        self.assertNotIn("houston", t["3"])                  # accuracy 6: no city tag
        self.assertEqual(t["4"], ["me", "photo"])            # no geo, date unknown, no camera

    def test_camera_albums_and_cap(self):
        albums = {(r.photo_id, r.target) for r in self.plan() if r.action == "add_to_album"}
        self.assertIn(("1", "Landscapes"), albums)
        self.cfg["standard_tags"]["max_tags_per_photo"] = 4
        t = self.tags()
        self.assertEqual(t["1"], ["y2019", "m07", "d17", "japan"])
        self.assertEqual(t["3"], ["unitedstates", "americas"])   # its 2 existing tags leave room for 2

    def test_disabled_by_default(self):
        self.cfg = from_dict({**CFG, "vision": {**CFG["vision"], "enabled": False}})
        self.assertEqual(self.tags(), {})


if __name__ == "__main__":
    unittest.main()
