"""Model-written descriptions: blank ones, re-wording, and applying them."""
import os
import tempfile
import unittest

from fakes import FakeFlickr, fake_geocoder, FakeVision, CFG
from flickr_organizer.apply import apply_changes
from flickr_organizer.classify import classify_photos, fingerprint, pending_photos
from flickr_organizer.config import from_dict
from flickr_organizer.planner import build_plan
from flickr_organizer.store import Store
from flickr_organizer.sync import sync


class DescribeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = from_dict({**CFG, "geo": {**CFG["geo"], "enabled": False},
                              "vision": {**CFG["vision"], "extra_tags": {"enabled": False},
                                         "describe": {"enabled": True}}})
        self.store = Store(os.path.join(self.tmp, "db.sqlite"))
        self.fl = FakeFlickr()
        self.fl.photos[1]["description"] = {"_content": "Written by me"}
        self.fl.photos[0]["title"] = "Ramen in Tokyo"
        sync(self.fl, self.store, log=lambda *a: None)
        self.vision = FakeVision("m1")
        self.classify()

    def classify(self):
        vcfg = self.cfg["vision"]
        todo = pending_photos(self.store, vcfg, fingerprint(vcfg, self.vision.key))
        classify_photos(self.store, self.vision, vcfg, todo, log=lambda *a: None,
                        downloader=lambda url: (b"jpg", "image/jpeg"))
        return todo

    def descriptions(self):
        rows = build_plan(self.cfg, self.store, self.vision.key)
        return {r.photo_id: r.new for r in rows if r.action == "set_description"}

    def test_only_blank_descriptions(self):
        got = self.descriptions()
        self.assertEqual(got["1"], "A steaming bowl of ramen.")
        self.assertNotIn("2", got)                          # has your own description
        self.assertEqual(set(got), {"1", "3", "4", "5"})

    def test_disabled_by_default_and_cache_kept(self):
        self.cfg["vision"]["describe"]["enabled"] = False
        self.assertEqual(self.descriptions(), {})
        self.assertEqual(self.classify(), [])               # toggling off doesn't re-classify

    def test_enabling_later_reclassifies_once(self):
        self.store.db.execute("DELETE FROM classifications")
        self.cfg["vision"]["describe"]["enabled"] = False
        self.classify()
        self.cfg["vision"]["describe"]["enabled"] = True
        self.assertEqual(len(self.classify()), 5)           # old results had no description
        self.assertEqual(self.classify(), [])

    def test_apply_edit_reject_and_keep_title(self):
        changes = build_plan(self.cfg, self.store, self.vision.key)
        for c in changes:
            if c.action != "set_description":
                continue
            if c.photo_id == "1":
                c.edited = "Tonkotsu ramen."
            if c.photo_id == "3":
                c.approved = False
        self.fl.photos[3]["description"] = {"_content": "Wrote my own meanwhile"}   # on Flickr, after the plan
        apply_changes(self.fl, self.store, changes, log=lambda *a: None)
        by_id = {p["id"]: p for p in self.fl.photos}
        self.assertEqual(by_id["1"]["description"]["_content"], "Tonkotsu ramen.")
        self.assertEqual(by_id["1"]["title"], "Ramen in Tokyo")      # title sent back unchanged
        self.assertEqual(by_id["3"]["description"]["_content"], "")  # rejected
        self.assertEqual(by_id["4"]["description"]["_content"], "Wrote my own meanwhile")  # not overwritten
        self.assertEqual(by_id["5"]["description"]["_content"], "Something else.")
        sync(self.fl, self.store, log=lambda *a: None)
        self.assertEqual(self.descriptions(), {})           # applied, rejected, or now has one


    def test_rebuild_rewords_descriptions_this_tool_wrote(self):
        apply_changes(self.fl, self.store, [c for c in build_plan(self.cfg, self.store, self.vision.key)
                                            if c.photo_id == "1"], log=lambda *a: None)
        self.store.db.execute("UPDATE decisions SET at = at - 10")             # applied a while ago
        self.store.db.commit()
        sync(self.fl, self.store, log=lambda *a: None)
        self.assertEqual(self.descriptions(), {"3": "Something else.", "4": "Something else.", "5": "Something else."})
        self.cfg["vision"]["describe"]["instructions"] = "Write it like a haiku, {max_words} words."
        self.classify()                                                       # the brief changed: re-run
        rows = {r.photo_id: r for r in build_plan(self.cfg, self.store, self.vision.key)
                if r.action == "set_description"}
        self.assertEqual(rows["1"].new, "Steam curls over the ramen.")
        self.assertEqual(rows["1"].old, "A steaming bowl of ramen.")          # what it replaces
        self.assertNotIn("2", rows)                                           # "Written by me" is yours
        apply_changes(self.fl, self.store, [rows["1"]], log=lambda *a: None)
        self.assertEqual(self.fl.photos[0]["description"]["_content"], "Steam curls over the ramen.")
        # You edit it on Flickr afterwards: it's yours now, even after another brief change.
        self.fl.photos[0]["description"] = {"_content": "Tonkotsu at midnight."}
        sync(self.fl, self.store, log=lambda *a: None)
        self.cfg["vision"]["describe"]["instructions"] = "Write it like a haiku again, {max_words} words."
        self.classify()
        self.assertNotIn("1", self.descriptions())

    def test_retitle_and_describe_share_one_call(self):
        import flickr_organizer.rename as rename_mod
        orig, rename_mod.country_names = rename_mod.country_names, lambda cc: ["United States"]
        self.addCleanup(setattr, rename_mod, "country_names", orig)
        self.cfg["rename"]["enabled"] = True
        rows = build_plan(self.cfg, self.store, self.vision.key, geocoder=fake_geocoder)
        self.assertEqual({r.action for r in rows if r.photo_id == "5"}, {"set_title", "set_description"})
        before = len(self.fl.calls)
        apply_changes(self.fl, self.store, rows, log=lambda *a: None)
        calls = [p for m, p in self.fl.calls[before:] if m == "flickr.photos.setMeta" and p["photo_id"] == "5"]
        self.assertEqual(calls, [{"photo_id": "5", "title": "Austin, TX", "description": "Something else."}])
        self.assertEqual(self.store.decisions()[("5", "set_title", "Austin, TX")], "applied")


if __name__ == "__main__":
    unittest.main()
