"""Model-written descriptions: proposed for every photo, settled per proposal."""
import os
import tempfile
import unittest

from fakes import FakeFlickr, fake_geocoder, FakeVision, CFG
from coconut_barometer.apply import apply_changes
from coconut_barometer.classify import classify_photos, fingerprint, pending_photos
from coconut_barometer.config import from_dict
from coconut_barometer.planner import build_plan
from coconut_barometer.store import Store
from coconut_barometer.sync import sync


class DescribeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = from_dict(CFG)
        self.store = Store(os.path.join(self.tmp, "db.duckdb"))
        self.fl = FakeFlickr()
        self.fl.photos[1]["description"] = {"_content": "Written by me"}
        self.fl.photos[2]["description"] = {"_content": "Something else."}   # already says it
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

    def rows(self):
        return {r.photo_id: r for r in build_plan(self.cfg, self.store, self.vision.key)
                if r.action == "set_description"}

    def descriptions(self):
        return {pid: r.new for pid, r in self.rows().items()}

    def test_every_photo_gets_one(self):
        rows = self.rows()
        self.assertEqual(rows["1"].new, "A steaming bowl of ramen.")
        self.assertEqual((rows["2"].new, rows["2"].old), ("Something else.", "Written by me"))   # yours: you decide
        self.assertNotIn("3", rows)                          # already says the same
        self.assertEqual(set(rows), {"1", "2", "4", "5"})

    def test_apply_edit_reject_and_keep_title(self):
        changes = build_plan(self.cfg, self.store, self.vision.key)
        for c in changes:
            if c.action == "set_title":
                c.approved = False
            if c.action != "set_description":
                continue
            if c.photo_id == "1":
                c.edited = "Tonkotsu ramen."
            if c.photo_id == "2":
                c.approved = False
        self.fl.photos[3]["description"] = {"_content": "Wrote my own meanwhile"}   # on Flickr, after the plan
        apply_changes(self.fl, self.store, changes, log=lambda *a: None)
        by_id = {p["id"]: p for p in self.fl.photos}
        self.assertEqual(by_id["1"]["description"]["_content"], "Tonkotsu ramen.")
        self.assertEqual(by_id["1"]["title"], "Ramen in Tokyo")      # title sent back unchanged
        self.assertEqual(by_id["2"]["description"]["_content"], "Written by me")   # unticked
        self.assertEqual(by_id["4"]["description"]["_content"], "Wrote my own meanwhile")  # not overwritten
        self.assertEqual(by_id["5"]["description"]["_content"], "Something else.")
        sync(self.fl, self.store, log=lambda *a: None)
        # No memory: re-planned, every one whose text differs from the model's is proposed again.
        self.assertEqual(set(self.descriptions()), {"1", "2", "4"})

    def test_a_new_brief_proposes_again(self):
        apply_changes(self.fl, self.store, [c for c in build_plan(self.cfg, self.store, self.vision.key)
                                            if c.photo_id == "1" and c.action == "set_description"],
                      log=lambda *a: None)
        sync(self.fl, self.store, log=lambda *a: None)
        self.assertNotIn("1", self.descriptions())                            # applied
        self.cfg["vision"]["describe"]["instructions"] = "Write it like a haiku, {max_words} words."
        self.classify()                                                       # the brief changed: re-run
        rows = self.rows()
        self.assertEqual((rows["1"].new, rows["1"].old), ("Steam curls over the ramen.", "A steaming bowl of ramen."))

    def test_title_and_description_share_one_call(self):
        rows = [r for r in build_plan(self.cfg, self.store, self.vision.key, geocoder=fake_geocoder)
                if r.action in ("set_title", "set_description")]
        self.assertEqual({r.action for r in rows if r.photo_id == "5"}, {"set_title", "set_description"})
        before = len(self.fl.calls)
        apply_changes(self.fl, self.store, rows, log=lambda *a: None)
        calls = [p for m, p in self.fl.calls[before:] if m == "flickr.photos.setMeta" and p["photo_id"] == "5"]
        self.assertEqual(calls, [{"photo_id": "5", "title": "Quiet Corner", "description": "Something else."}])
        self.assertEqual(self.store.decisions()[("5", "set_title", "Quiet Corner")], "applied")


if __name__ == "__main__":
    unittest.main()
