"""Undoing an apply: what it replaced comes back, unless Flickr changed since."""
import os
import tempfile
import unittest

from fakes import FakeFlickr, fake_geocoder, FakeVision, CFG
from coconut_barometer.config import from_dict
from coconut_barometer.pipeline import process
from coconut_barometer.review import ReviewApp
from coconut_barometer.store import Store
from coconut_barometer.sync import sync


class UndoTest(unittest.TestCase):
    def setUp(self):
        self.cfg = from_dict(CFG)
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))
        self.fl = FakeFlickr()
        self.fl.photos[0]["tags"] = "mine"
        sync(self.fl, self.store, log=lambda *a: None)
        process(self.cfg, self.store, self.fl, FakeVision("m1"), geocoder=fake_geocoder, log=lambda *a: None,
                downloader=lambda url: (b"jpg", "image/jpeg"))
        self.app = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg)

    def apply_all(self, pid):
        self.app.decide(pid, [{"approve": True} for _ in self.store.queue_item(pid)["changes"]])

    def test_everything_written_comes_back(self):
        self.apply_all("1")
        ph = self.fl.photos[0]
        self.assertNotEqual((ph["title"], ph["tags"]), ("Ramen in Tokyo", "mine"))
        self.assertIn("1", self.fl.sets["102"]["photos"])                      # Food
        (row,) = self.app.applied()
        self.assertTrue(row["can_undo"])
        self.assertIn("album Food", row["what"])
        self.assertEqual(self.app.undo("1"), {"status": "done"})
        self.assertEqual((ph["title"], ph["description"]["_content"], ph["tags"]), ("Ramen in Tokyo", "", "mine"))
        self.assertNotIn("1", self.fl.sets["102"]["photos"])
        self.assertEqual(self.store.photo("1")["title"], "Ramen in Tokyo")
        self.assertNotIn("102", self.store.memberships().get("1", set()))
        self.assertEqual(self.store.decisions()[("1", "add_to_album", "Food")], "undone")   # for Analytics
        self.assertFalse(self.app.applied()[0]["can_undo"])
        with self.assertRaises(KeyError):                                       # nothing left to undo
            self.app.undo("1")

    def test_what_changed_on_flickr_since_is_left_alone(self):
        self.apply_all("1")
        self.fl.photos[0]["title"] = "Retitled by hand"
        out = self.app.undo("1")
        self.assertIn("title changed on Flickr since", out["error"])
        self.assertEqual(self.fl.photos[0]["title"], "Retitled by hand")
        self.assertEqual(self.fl.photos[0]["tags"], "mine")                     # the rest still undone

    def test_an_album_made_for_the_photo_goes_with_it(self):
        self.cfg["albums"]["create_missing"] = True
        self.cfg["geo"]["cities"].append({"album": "Tokyo", "lat": 35.68, "lon": 139.69, "radius_km": 30})
        process(self.cfg, self.store, self.fl, FakeVision("m1"), photo_ids=["1"], geocoder=fake_geocoder,
                log=lambda *a: None, downloader=lambda url: (b"jpg", "image/jpeg"))
        self.apply_all("1")
        made = next(k for k, v in self.fl.sets.items() if v["title"] == "Tokyo")
        self.app.undo("1")
        self.assertNotIn(made, self.fl.sets)

    def test_a_location_set_is_removed(self):
        self.store.queue_put("4", "pending", [{"action": "set_location", "photo_id": "4",
                                               "new": {"lat": 35.0, "lon": 135.0, "accuracy": 11}}])
        self.app.decide("4", [{"approve": True}])
        self.assertEqual(self.fl.photos[3]["latitude"], 35.0)
        self.app.undo("4")
        self.assertEqual(self.fl.photos[3]["latitude"], 0)
        self.assertIsNone(self.store.photo("4")["lat"])

    def test_applying_some_leaves_the_rest_in_review(self):
        changes = self.store.queue_item("1")["changes"]
        self.app.decide("1", [{"approve": True}])                               # a choice for the first only
        left = self.store.queue_item("1")
        self.assertEqual((left["status"], left["changes"]), ("pending", changes[1:]))   # the rest still in review


if __name__ == "__main__":
    unittest.main()
