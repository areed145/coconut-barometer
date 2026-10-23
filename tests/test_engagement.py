"""Engagement data: counts, snapshots, groups, comments and faves."""
import os
import tempfile
import unittest

from fakes import FakeFlickr
from coconut_barometer.review import ReviewApp
from coconut_barometer import social
from coconut_barometer.store import Store
from coconut_barometer.sync import sync


class SocialTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "db.duckdb"))
        self.fl = FakeFlickr()
        for ph, (views, faves, comments) in zip(self.fl.photos, [(100, 60, 1), (5, 0, 0), (7, 0, 2)]):
            ph.update(views=str(views), count_faves=str(faves), count_comments=str(comments))
        sync(self.fl, self.store, log=lambda *a: None)
        self.quiet = lambda *a: None

    def test_counts_and_daily_snapshot_from_sync(self):
        p = self.store.photo("1")
        self.assertEqual((p["views"], p["faves"], p["comments"]), (100, 60, 1))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM photo_stats").fetchone()[0], 3)
        self.assertIn("1234", self.store.db.execute("SELECT stats FROM account_stats").fetchone()[0])

    def test_viewer_shows_engagement_and_sorts_by_views(self):
        self.fl.photos[0]["license"] = "0"
        sync(self.fl, self.store, log=lambda *a: None)
        social.fetch_groups(self.fl, self.store, log=self.quiet)
        social.fetch_comments(self.fl, self.store, ["1"], log=self.quiet)
        for pid in ("1", "2", "3"):
            self.store.queue_put(pid, "pending", [])
        app = ReviewApp(self.store, self.fl, background=False)
        st = app.item("1")["stats"]
        self.assertEqual((st["views"], st["faves"], st["comments"], st["license"]), (100, 60, 1, "All Rights Reserved"))
        self.assertEqual([g["name"] for g in st["groups"]], ["Ramen Lovers"])
        self.assertEqual(st["recent_comments"][0]["text"], "Nice!")
        self.assertEqual(st["first_snapshot"]["views"], 100)
        self.assertEqual(app.queue("views"), ["1", "3", "2"])

    def test_groups_comments_faves_fetched_only_when_counts_change(self):
        self.assertEqual(social.fetch_groups(self.fl, self.store, log=self.quiet)["photos_in_pools"], 3)
        self.assertEqual(self.store.photo_groups("1"), ["Ramen Lovers"])
        todo = social.plan(self.store)
        self.assertEqual(sorted(todo["comments"]), ["1", "3"])
        self.assertEqual(todo["faves"], ["1"])
        social.fetch_comments(self.fl, self.store, todo["comments"], log=self.quiet)
        social.fetch_faves(self.fl, self.store, todo["faves"], log=self.quiet)
        self.assertEqual(self.store.db.execute("SELECT text FROM comments WHERE photo_id='1'").fetchone()[0], "Nice!")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM faves").fetchone()[0], 60)   # both pages
        self.assertEqual(social.plan(self.store), {"comments": [], "faves": []})              # nothing changed
        self.fl.photos[0]["count_comments"] = "2"                                              # a new comment
        sync(self.fl, self.store, log=lambda *a: None)
        self.assertEqual(social.plan(self.store)["comments"], ["1"])


if __name__ == "__main__":
    unittest.main()
