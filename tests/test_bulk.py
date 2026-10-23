"""Bulk review: one kind of change across many photos; and re-planning everything in review."""
import os
import tempfile
import unittest

from fakes import FakeFlickr, fake_geocoder, FakeVision, CFG
from coconut_barometer.config import from_dict
from coconut_barometer.pipeline import process
from coconut_barometer.review import ReviewApp
from coconut_barometer.store import Store
from coconut_barometer.sync import sync


class BulkTest(unittest.TestCase):
    def setUp(self):
        self.cfg = from_dict(CFG)
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))
        self.fl = FakeFlickr()
        sync(self.fl, self.store, log=lambda *a: None)
        process(self.cfg, self.store, self.fl, FakeVision("m1"), geocoder=fake_geocoder, log=lambda *a: None,
                downloader=lambda url: (b"jpg", "image/jpeg"))
        self.app = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg,
                             process_kw={"geocoder": fake_geocoder})

    def picks(self, kind, value=""):
        return [{"photo_id": i["photo_id"], "index": i["index"], "action": i["change"]["action"]}
                for i in self.app.bulk(kind, value)["items"]]

    def test_lists_one_kind_with_counts(self):
        out = self.app.bulk("album")
        self.assertEqual(out["counts"]["album"], out["total"])
        self.assertIn("title", out["counts"])
        self.assertIn("Food", out["groups"])
        self.assertEqual({i["change"]["new"] for i in self.app.bulk("album", "Food")["items"]}, {"Food"})

    def test_photos_still_being_processed_are_counted_not_listed(self):
        tags = self.app.bulk("tags")
        self.assertEqual(tags["busy"], 0)
        pid = tags["items"][0]["photo_id"]
        self.app.processing.add(pid)
        out = self.app.bulk("tags")
        self.assertEqual((out["busy"], out["total"]), (1, tags["total"] - 1))
        self.assertNotIn(pid, {i["photo_id"] for i in out["items"]})

    def test_apply_some_kind_and_the_rest_stays(self):
        before = {pid: len(self.store.queue_item(pid)["changes"]) for pid in self.store.queue_ids(["pending"])}
        out = self.app.bulk_decide("apply", self.picks("album", "Food"))
        self.assertEqual(out["changes"], 1)
        self.assertIn("1", self.fl.sets["102"]["photos"])
        left = self.store.queue_item("1")["changes"]
        self.assertEqual(len(left), before["1"] - 1)                          # its title etc. still to review
        self.assertNotIn(("add_to_album", "Food"), {(c["action"], str(c["new"])) for c in left})
        self.assertTrue(self.app.applied()[0]["can_undo"])                    # undoable like any apply

    def test_drop_and_stale_picks(self):
        picks, calls = self.picks("title"), len(self.fl.calls)
        stale = [{**picks[0], "action": "set_description"}]                  # re-planned since: skipped
        self.assertEqual(self.app.bulk_decide("drop", stale)["skipped"], 1)
        self.app.bulk_decide("drop", picks)
        self.assertEqual(self.app.bulk("title")["total"], 0)
        self.assertEqual(len(self.fl.calls), calls)                           # nothing sent to Flickr

    def test_replan_uses_the_new_settings_without_calls(self):
        calls = len(self.fl.calls)
        self.cfg["tags"]["always_add"] = ["adam reeder"]
        self.assertEqual(self.app.replan()["status"], "done")
        self.assertEqual(len(self.fl.calls), calls)
        tags = next(c for c in self.store.queue_item("1")["changes"] if c["action"] == "set_tags")
        self.assertEqual(tags["new"][-1], "adam reeder")
        self.assertIn("set_title", {c["action"] for c in self.store.queue_item("1")["changes"]})   # vision kept

    def test_replan_keeps_vision_made_with_older_settings(self):
        self.cfg["vision"]["categories"]["food"]["description"] = "food, edited"   # a new fingerprint
        self.app.replan()
        self.assertIn("set_description", {c["action"] for c in self.store.queue_item("1")["changes"]})


if __name__ == "__main__":
    unittest.main()
