"""Sync, the bulk plan, CSV apply, and incremental runs."""
import csv
import os
import tempfile
import unittest

from fakes import photo, FakeFlickr, fake_geocoder, FakeVision, CFG
from flickr_organizer.apply import apply_plan
from flickr_organizer.classify import classify_photos, fingerprint, pending_photos
from flickr_organizer.config import from_dict
from flickr_organizer.planner import build_plan, read_plan_csv, write_plan_csv
from flickr_organizer.report import write_report
from flickr_organizer.store import Store
from flickr_organizer.sync import sync


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = from_dict(CFG)
        self.store = Store(os.path.join(self.tmp, "db.sqlite"))
        self.fl = FakeFlickr()
        sync(self.fl, self.store, log=lambda *a: None)
        self.vision = FakeVision("m1")
        vcfg = self.cfg["vision"]
        todo = pending_photos(self.store, vcfg, fingerprint(vcfg, self.vision.key))
        classify_photos(self.store, self.vision, vcfg, todo, log=lambda *a: None,
                        downloader=lambda url: (b"jpg", "image/jpeg"))

    def plan(self):
        return build_plan(self.cfg, self.store, self.vision.key, geocoder=fake_geocoder)

    def test_sync_paginates(self):
        self.assertEqual(len(self.store.photos()), 5)
        self.assertEqual(len(self.store.albums()), 6)
        self.assertEqual(self.store.memberships()["2"], {"101"})

    def test_plan_contents(self):
        rows = self.plan()
        albums = {(r.photo_id, r.target) for r in rows if r.action == "add_to_album"}
        self.assertIn(("1", "Japan"), albums)
        self.assertIn(("1", "Food"), albums)
        self.assertIn(("2", "Austin"), albums)
        self.assertIn(("2", "United States"), albums)
        self.assertNotIn(("2", "Texas"), albums)          # already a member
        self.assertIn(("3", "Texas"), albums)              # accuracy 6 is enough for state
        self.assertNotIn(("3", "Austin"), albums)          # Houston, and too coarse for city
        self.assertFalse(any(r.photo_id == "4" for r in rows if r.source.startswith("geo")))
        self.assertFalse(any(t == "Landscapes" for _, t in albums))  # below min_confidence
        tags = {r.photo_id: r.target for r in rows if r.action == "add_tags"}
        self.assertEqual(tags["1"], "food | ramen | noodle soup")
        self.assertNotIn("5", tags)                        # 'food' tag exists, low score

    def test_apply_and_incremental(self):
        rows = self.plan()
        path = os.path.join(self.tmp, "plan.csv")
        write_plan_csv(rows, path)
        write_report(rows, os.path.join(self.tmp, "plan.html"), "plan.csv")
        # Reviewer rejects photo 3 → Texas, and drops the "noodle soup" tag.
        edited = read_plan_csv(path)
        for r in edited:
            if r["photo_id"] == "3" and r["target"] == "Texas":
                r["approve"] = "n"
            if r["action"] == "add_tags" and r["photo_id"] == "1":
                r["target"] = "food | ramen"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=edited[0].keys()); w.writeheader(); w.writerows(edited)

        stats = apply_plan(self.fl, self.store, path, log=lambda *a: None)
        self.assertEqual(stats["failed"], 0)
        self.assertIn("1", self.fl.sets["102"]["photos"])      # Food
        self.assertIn("2", self.fl.sets["103"]["photos"])      # Austin
        self.assertNotIn("3", self.fl.sets["101"]["photos"])   # rejected
        self.assertIn("ramen", self.fl.photos[0]["tags"])
        self.assertNotIn("noodle", self.fl.photos[0]["tags"])

        # Next run: re-sync, re-plan → nothing applied or rejected comes back.
        sync(self.fl, self.store, log=lambda *a: None)
        again = self.plan()
        self.assertEqual(again, [], f"unexpected rows: {[(r.photo_id, r.target) for r in again]}")

    def test_new_photo_is_incremental(self):
        path = os.path.join(self.tmp, "p.csv")
        write_plan_csv(self.plan(), path)
        apply_plan(self.fl, self.store, path, log=lambda *a: None)
        self.fl.photos.append(photo("6", 35.0, 135.7, title="Ramen in Kyoto"))
        sync(self.fl, self.store, log=lambda *a: None)
        vcfg = self.cfg["vision"]
        todo = pending_photos(self.store, vcfg, fingerprint(vcfg, self.vision.key))
        self.assertEqual([p["id"] for p in todo], ["6"])   # only the new one gets classified
        classify_photos(self.store, self.vision, vcfg, todo, log=lambda *a: None,
                        downloader=lambda url: (b"jpg", "image/jpeg"))
        self.assertEqual({r.photo_id for r in self.plan()}, {"6"})

    def test_create_missing_album(self):
        self.cfg["albums"]["create_missing"] = True
        self.cfg["geo"]["cities"].append({"album": "Houston", "lat": 29.76, "lon": -95.37, "radius_km": 30})
        self.fl.photos[2]["accuracy"] = 16
        sync(self.fl, self.store, log=lambda *a: None)
        rows = [r for r in self.plan() if r.target == "Houston"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].album_id, "")
        path = os.path.join(self.tmp, "c.csv")
        write_plan_csv(rows, path)
        stats = apply_plan(self.fl, self.store, path, log=lambda *a: None)
        self.assertEqual(stats["albums_created"], 1)
        self.assertTrue(any(s["title"] == "Houston" and "3" in s["photos"] for s in self.fl.sets.values()))

    def test_dry_run_changes_nothing(self):
        path = os.path.join(self.tmp, "d.csv")
        write_plan_csv(self.plan(), path)
        before = len(self.fl.calls)
        apply_plan(None, self.store, path, dry_run=True, log=lambda *a: None)
        self.assertEqual(len(self.fl.calls), before)
        self.assertEqual(self.store.counts()["applied"], 0)

    def test_changes_applied_during_a_sync_survive_it(self):
        store, fl = self.store, self.fl
        real_call = fl.call

        def call(method, **p):   # the review tool applies a description while the sync runs
            out = real_call(method, **p)
            if method == "flickr.people.getPhotos" and int(p.get("page", 1)) == 1:
                store.record("1", "set_description", "Written mid-sync", "applied")
                store.record("1", "add_tag", "midsync", "applied")
            return out
        fl.call = call
        sync(fl, store, log=lambda *a: None)
        self.assertEqual(store.photo("1")["description"], "Written mid-sync")
        self.assertIn("midsync", store.photo("1")["tags"].split())

    def test_fingerprint_changes_with_categories(self):
        vcfg = self.cfg["vision"]
        a = fingerprint(vcfg, "claude:x")
        vcfg["categories"]["food"]["description"] = "food, more specific"
        self.assertNotEqual(a, fingerprint(vcfg, "claude:x"))


if __name__ == "__main__":
    unittest.main()
