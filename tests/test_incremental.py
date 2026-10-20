"""Incremental sync, what's due when, and the per-day stats history."""
import datetime as dt
import os
import tempfile
import time
import unittest

from fakes import photo, FakeFlickr, CFG
from flickr_organizer import stats_history
from flickr_organizer.config import from_dict
from flickr_organizer.incremental import sync_incremental
from flickr_organizer.schedule import due, run_due
from flickr_organizer.store import Store
from flickr_organizer.sync import sync


class IncrementalTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.sqlite"))
        self.fl = FakeFlickr()
        sync(self.fl, self.store, log=lambda *a: None)
        self.fl.clock = int(time.time())          # Flickr's clock is "now" from here on
        self.quiet = lambda *a: None

    def incremental(self):
        before = len(self.fl.calls)
        out = sync_incremental(self.fl, self.store, log=self.quiet)
        return out, [m for m, _ in self.fl.calls[before:]]

    def test_nothing_changed_is_cheap(self):
        out, calls = self.incremental()
        self.assertEqual((out["updated"], out["new"], out["albums_changed"], out["activity"]), (0, 0, 0, 0))
        self.assertNotIn("flickr.photosets.getPhotos", calls)          # no album re-read
        self.assertNotIn("flickr.people.getPhotos", calls)             # no full photo list

    def test_edits_uploads_and_album_changes_are_picked_up(self):
        self.fl.photos[0]["title"] = "Edited on Flickr"
        self.fl._touch(photo_id="1")
        self.fl.photos.append({**photo("6", title="New upload"), "lastupdate": str(self.fl.clock)})
        self.fl.call("flickr.photosets.addPhoto", photoset_id="105", photo_id="2")   # Landscapes
        out, calls = self.incremental()
        self.assertEqual(self.store.photo("1")["title"], "Edited on Flickr")
        self.assertEqual(self.store.photo("6")["title"], "New upload")
        self.assertEqual((out["new"], out["albums_changed"]), (1, 1))
        self.assertIn("105", self.store.memberships()["2"])
        reread = [p["photoset_id"] for m, p in self.fl.calls if m == "flickr.photosets.getPhotos"][-1:]
        self.assertEqual(reread, ["105"])                              # only the changed album

    def test_deletions_are_found_from_the_photo_count(self):
        del self.fl.photos[3]                                          # photo 4 deleted on Flickr
        out, calls = self.incremental()
        self.assertEqual(out["deleted"], 1)
        self.assertIsNone(self.store.photo("4"))
        self.assertIn("flickr.people.getPhotos", calls)

    def test_new_comments_from_activity(self):
        self.fl.activity = [{"id": "1", "comments": 1, "faves": 0, "views": 9}]
        out, _ = self.incremental()
        self.assertEqual(out["activity"], 1)
        self.assertEqual(self.store.photo("1")["comments"], 1)
        self.assertEqual(self.store.engagement("1")["recent_comments"][0]["text"], "Nice!")


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.cfg = from_dict({**CFG, "schedule": {"stats": False}})
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.sqlite"))
        self.fl = FakeFlickr()

    def test_full_then_incremental_then_daily_photo_list(self):
        self.assertEqual(due(self.cfg, self.store), ["full"])          # never synced
        run_due(self.cfg, self.fl, self.store, log=lambda *a: None)
        now = time.time()
        self.assertEqual(due(self.cfg, self.store, now + 3600), ["incremental"])
        self.assertEqual(due(self.cfg, self.store, now + 25 * 3600), ["photo_list", "incremental"])
        self.assertEqual(due(self.cfg, self.store, now + 8 * 86400), ["full"])
        out = run_due(self.cfg, self.fl, self.store, force="photo_list", log=lambda *a: None)
        self.assertGreater(out["calls"], 0)
        self.assertEqual(out["ran"], ["photo_list", "incremental"])


class StatsHistoryTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.sqlite"))
        sync(FakeFlickr(), self.store, log=lambda *a: None)
        self.fl = FakeFlickr()
        today = dt.datetime.now(dt.timezone.utc).date()
        self.day = lambda back: (today - dt.timedelta(days=back)).isoformat()
        # Flickr has stats for the last 5 days only.
        for back in range(1, 6):
            self.fl.daily[self.day(back)] = {"total": 100 * back, "photos": [("1", 10 * back, 1, 2), ("2", back, 0, 0),
                                                                              ("3", 1, 0, 0)]}

    def test_yesterday_then_backfill_until_flickr_has_none(self):
        out = stats_history.update(self.fl, self.store, max_backfill=2, log=lambda *a: None)
        self.assertEqual(out["days"], 4)                                # the 2 recent days + 2 backfill
        self.assertEqual(self.store.daily_totals()[-1]["total"], 100)
        stats_history.update(self.fl, self.store, max_backfill=10, log=lambda *a: None)
        days = self.store.stats_days()
        self.assertEqual(days[self.day(6)], "none")                     # where Flickr ran out
        self.assertEqual(sum(1 for s in days.values() if s == "done"), 5)
        before = len(self.fl.calls)
        stats_history.update(self.fl, self.store, max_backfill=10, log=lambda *a: None)
        self.assertEqual(len(self.fl.calls), before)                    # nothing left; recent days re-fetched later
        top = self.store.top_photos(self.day(5))
        self.assertEqual((top[0]["photo_id"], top[0]["views"]), ("1", 150))
        self.assertEqual(len(self.store.photo_history("1")), 5)

    def test_recent_days_refetched_until_final(self):
        stats_history.update(self.fl, self.store, max_backfill=0, log=lambda *a: None)
        now = time.time()
        self.assertEqual(stats_history.days_due(self.store, 0, now=now), [])           # just fetched
        later = now + 7 * 3600              # past the re-fetch interval (and maybe past midnight GMT)
        yesterday = (dt.datetime.fromtimestamp(later, dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
        self.assertEqual(stats_history.days_due(self.store, 0, now=later)[:1], [yesterday])


if __name__ == "__main__":
    unittest.main()
