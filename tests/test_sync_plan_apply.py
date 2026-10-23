"""Sync, planning, applying Changes, and incremental runs."""
import os
import tempfile
import unittest

from fakes import photo, FakeFlickr, fake_geocoder, FakeVision, CFG
from coconut_barometer.apply import apply_changes
from coconut_barometer.classify import classify_photos, fingerprint, pending_photos
from coconut_barometer.config import from_dict
from coconut_barometer.flickr_client import FlickrCallError
from coconut_barometer.planner import build_plan
from coconut_barometer.store import Store
from coconut_barometer.sync import sync


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = from_dict({**CFG, "review": {"missing_location": False}})   # (test_geoguess.py has those)
        self.store = Store(os.path.join(self.tmp, "db.duckdb"))
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
        albums = {(r.photo_id, r.new) for r in rows if r.action == "add_to_album"}
        self.assertIn(("1", "Japan"), albums)
        self.assertIn(("1", "Food"), albums)
        self.assertIn(("2", "Austin"), albums)
        self.assertIn(("2", "United States"), albums)
        self.assertNotIn(("2", "Texas"), albums)          # already a member
        self.assertIn(("3", "Texas"), albums)              # accuracy 6 is enough for state
        self.assertNotIn(("3", "Austin"), albums)          # Houston, and too coarse for city
        self.assertFalse(any(r.photo_id == "4" for r in rows if r.source.startswith("geo")))
        self.assertFalse(any(t == "Landscapes" for _, t in albums))  # below min_confidence
        tags = {r.photo_id: r.new for r in rows if r.action == "add_tags"}
        self.assertEqual(tags["1"], ["food", "ramen", "noodle soup"])
        self.assertNotIn("5", tags)                        # 'food' tag exists, low score

    def test_apply_and_incremental(self):
        changes = self.plan()
        # Reviewer unticks photo 3 → Texas, and drops the "noodle soup" tag.
        for c in changes:
            if c.photo_id == "3" and c.new == "Texas":
                c.approved = False
            if c.action == "add_tags" and c.photo_id == "1":
                c.edited = ["food", "ramen"]

        stats = apply_changes(self.fl, self.store, changes, log=lambda *a: None)
        self.assertEqual(stats["failed"], 0)
        self.assertIn("1", self.fl.sets["102"]["photos"])      # Food
        self.assertIn("2", self.fl.sets["103"]["photos"])      # Austin
        self.assertNotIn("3", self.fl.sets["101"]["photos"])   # unticked
        self.assertIn("ramen", self.fl.photos[0]["tags"])
        self.assertNotIn("noodle", self.fl.photos[0]["tags"])

        # Next run: re-sync, re-plan → what's on Flickr now isn't proposed; what you left out is, afresh.
        sync(self.fl, self.store, log=lambda *a: None)
        again = {(c.photo_id, c.action, tuple(c.new) if isinstance(c.new, list) else c.new) for c in self.plan()}
        self.assertEqual(again, {("3", "add_to_album", "Texas"), ("1", "add_tags", ("noodle soup",))})

    def test_new_photo_is_incremental(self):
        apply_changes(self.fl, self.store, self.plan(), log=lambda *a: None)
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
        rows = [c for c in self.plan() if c.new == "Houston"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].album_id, "")
        stats = apply_changes(self.fl, self.store, rows, log=lambda *a: None)
        self.assertEqual(stats["albums_created"], 1)
        self.assertTrue(any(s["title"] == "Houston" and "3" in s["photos"] for s in self.fl.sets.values()))

    def test_dry_run_changes_nothing(self):
        changes = self.plan()
        before = len(self.fl.calls)
        apply_changes(None, self.store, changes, dry_run=True, log=lambda *a: None)
        self.assertEqual(len(self.fl.calls), before)
        self.assertEqual(self.store.counts()["applied"], 0)

    def test_failed_changes_are_proposed_again(self):
        albums = lambda: {c.new for c in self.plan() if c.photo_id == "1" and c.action == "add_to_album"}
        food = [c for c in self.plan() if c.photo_id == "1" and c.new == "Food"]
        real_call = self.fl.call

        def flaky(method, **p):
            if method == "flickr.photosets.addPhoto":
                raise FlickrCallError(method, 500, "server error")
            return real_call(method, **p)
        self.fl.call = flaky
        self.assertEqual(apply_changes(self.fl, self.store, food, log=lambda *a: None)["failed"], 1)
        self.assertIn("Food", albums())                    # a failure doesn't settle it
        self.fl.call = real_call
        apply_changes(self.fl, self.store, food, log=lambda *a: None)
        self.assertNotIn("Food", albums())

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


class ParseInfoThumbTest(unittest.TestCase):
    def test_thumbnail_refreshed_from_get_info(self):
        from coconut_barometer.sync import parse_info
        info = {"id": "9", "server": "65535", "secret": "abc", "media": "video",
                "title": {"_content": "t"}, "description": {"_content": ""}}
        out = parse_info(info, "url_m")
        self.assertEqual(out["thumb_url"], "https://live.staticflickr.com/65535/9_abc.jpg")    # url_m: 500 px
        self.assertEqual(out["media"], "video")
        self.assertEqual(parse_info(info, "url_z")["thumb_url"], "https://live.staticflickr.com/65535/9_abc_z.jpg")
        self.assertNotIn("thumb_url", parse_info(info))                    # applying changes: thumbnail left alone
        self.assertNotIn("thumb_url", parse_info({"title": {}, "description": {}}, "url_m"))

    def test_location_privacy(self):
        from coconut_barometer.sync import normalize_photo, parse_info
        from fakes import photo
        self.assertEqual(normalize_photo({**photo("1", 35.6, 139.7), "geo_is_public": "0"}, "url_m")["geo_public"], 0)
        self.assertEqual(normalize_photo(photo("1", 35.6, 139.7), "url_m")["geo_public"], 1)
        self.assertIsNone(normalize_photo(photo("1"), "url_m")["geo_public"])           # no location
        info = {"title": {}, "description": {}, "location": {"latitude": "1.5", "longitude": "2.5"},
                "geoperms": {"ispublic": 0, "iscontact": 1, "isfriend": 0, "isfamily": 0}}
        self.assertEqual(parse_info(info)["geo_public"], 0)
