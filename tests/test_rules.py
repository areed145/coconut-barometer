"""The rules package: priority between rules, and costly lookups only when needed."""
import os
import tempfile
import unittest
from unittest import mock

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
        ctx = Context(from_dict(CFG), self.store, None)
        with mock.patch("coconut_barometer.rules.base.MAX_TAGS_PER_PHOTO", 2):
            changes = resolve(ctx, [AlbumWant("1", "Food", "geo:city", reason="first"),
                                    AlbumWant("1", "food", "vision:food", reason="second"),
                                    TagWant("1", "Ramen", "vision:tags"), TagWant("1", "ramen", "std:x"),
                                    TagWant("1", "noodles", "vision:tags"), TagWant("1", "soup", "std:x")])
        album = next(c for c in changes if c.action == "add_to_album")
        self.assertEqual((album.new, album.source, album.reason), ("Food", "geo:city", "first"))
        tags = next(c for c in changes if c.action == "set_tags")
        self.assertEqual(tags.new, ["ramen", "noodles"])          # deduped, then capped at 2

    def test_always_add_and_always_remove(self):
        self.store.update_photo("4", tags="mine wwwexamplecom")
        cfg = from_dict({**CFG, "tags": {"always_add": ["Adam Reeder"], "always_remove": ["www*", "noodles"]}})
        ctx = Context(cfg, self.store, None)
        changes = {c.photo_id: c for c in resolve(ctx, [TagWant("1", "ramen", "vision:tags"),
                                                        TagWant("1", "noodles", "vision:tags")])}
        self.assertEqual(changes["1"].new, ["ramen", "adam reeder"])        # removed even though proposed; added last
        self.assertEqual(changes["4"].new, ["mine", "adam reeder"])          # nothing proposed: keeps its own, less www*
        self.assertEqual(changes["4"].source, "always:add,always:remove")
        self.store.update_photo("4", tags="mine adamreeder")
        changes = {c.photo_id: c for c in resolve(Context(cfg, self.store, None), [])}
        self.assertNotIn("4", changes)                                       # already right: nothing to change

if __name__ == "__main__":
    unittest.main()
