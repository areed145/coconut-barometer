"""The rules package: priority between rules, and costly lookups only when needed."""
import os
import tempfile
import unittest

from fakes import FakeFlickr, CFG
from coconut_barometer.config import from_dict
from coconut_barometer.planner import build_plan
from coconut_barometer.rules import Context, AlbumWant, TagWant, resolve
from coconut_barometer.store import Store
from coconut_barometer.sync import sync


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))
        sync(FakeFlickr(), self.store, log=lambda *a: None)

    def test_no_geotags_never_geocode(self):
        for row in self.store.photos():
            self.store.db.execute("UPDATE photos SET lat=NULL, lon=NULL WHERE id=?", (row["id"],))
        self.store.db.commit()

        def geocoder(points):
            raise AssertionError("geocoded although no photo has a place")
        build_plan(from_dict(CFG), self.store, None, geocoder=geocoder)

    def test_first_want_wins_and_tags_respect_the_cap(self):
        cfg = from_dict({**CFG, "standard_tags": {"max_tags_per_photo": 2}})
        ctx = Context(cfg, self.store, None)
        changes = resolve(ctx, [AlbumWant("1", "Food", "geo:city", reason="first"),
                                AlbumWant("1", "food", "vision:food", reason="second"),
                                TagWant("1", "Ramen", "vision:tags"), TagWant("1", "ramen", "std:x"),
                                TagWant("1", "noodles", "vision:tags"), TagWant("1", "soup", "std:x")])
        album = next(c for c in changes if c.action == "add_to_album")
        self.assertEqual((album.new, album.source, album.reason), ("Food", "geo:city", "first"))
        tags = next(c for c in changes if c.action == "add_tags")
        self.assertEqual(tags.new, ["ramen", "noodles"])          # deduped, then capped at 2


if __name__ == "__main__":
    unittest.main()
