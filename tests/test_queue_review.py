"""The per-photo queue (process) and the review tool."""
import os
import tempfile
import unittest

from fakes import photo, FakeFlickr, fake_geocoder, FakeVision, CFG
from flickr_organizer.config import from_dict
from flickr_organizer.pipeline import process
from flickr_organizer.review import ReviewApp
from flickr_organizer.store import Store
from flickr_organizer.sync import sync


class QueueReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = from_dict({**CFG, "vision": {**CFG["vision"], "describe": {"enabled": True}}})
        self.store = Store(os.path.join(self.tmp, "db.sqlite"))
        self.fl = FakeFlickr()
        sync(self.fl, self.store, log=lambda *a: None)
        self.vision = FakeVision("m1")
        self.stats = self.process()
        self.app = ReviewApp(self.store, self.fl)

    def decide(self, pid, choices):
        """Approve/reject returns at once; the worker applies in the background."""
        self.assertEqual(self.app.decide(pid, choices), {"status": "applying"})
        self.app.wait()
        item = self.store.queue_item(pid)
        return {"status": item["status"], **item["result"]}

    def process(self, **kw):
        return process(self.cfg, self.store, self.fl, self.vision, geocoder=fake_geocoder, log=lambda *a: None,
                       downloader=lambda url: (b"jpg", "image/jpeg"), **kw)

    def changes(self, pid):
        return {(c["action"], tuple(c["new"]) if isinstance(c["new"], list) else c["new"])
                for c in self.store.queue_item(pid)["changes"]}

    def test_queue_is_built_per_photo_and_resumable(self):
        self.assertEqual(self.stats["queued"] + self.stats["nothing_to_change"], 5)
        self.assertEqual(self.store.camera_ids(), {"1", "2", "3", "4", "5"})      # EXIF fetched for each
        self.assertIn(("add_to_album", "Food"), self.changes("1"))
        self.assertIn(("set_description", "A steaming bowl of ramen."), self.changes("1"))
        self.assertEqual(self.process()["queued"], 0)                            # nothing re-done
        item = self.app.item("1")
        self.assertEqual(item["photo"]["title"], "Ramen in Tokyo")
        self.assertTrue(item["photo"]["image"].endswith("1_s_b.jpg"))

    def test_approve_one_by_one_applies_and_records(self):
        changes = self.store.queue_item("1")["changes"]
        choices = []
        for c in changes:
            if c["action"] == "add_tags":
                choices.append({"approve": True, "value": ["food", "ramen"]})        # dropped "noodle soup"
            elif c["action"] == "set_description":
                choices.append({"approve": True, "value": "Tonkotsu ramen."})
            elif c["new"] == "Japan":
                choices.append({"approve": False})
            else:
                choices.append({"approve": True})
        r = self.decide("1", choices)
        self.assertEqual(r["status"], "applied")
        self.assertIn("1", self.fl.sets["102"]["photos"])                          # Food
        self.assertNotIn("1", self.fl.sets["100"]["photos"])                       # Japan rejected
        self.assertIn("ramen", self.fl.photos[0]["tags"])
        self.assertNotIn("noodle", self.fl.photos[0]["tags"])
        self.assertEqual(self.fl.photos[0]["description"]["_content"], "Tonkotsu ramen.")
        tags = next(o for o in r["changes"] if o["action"] == "add_tags")["tags"]
        self.assertEqual(tags, {"food": "applied", "ramen": "applied", "noodle soup": "rejected"})
        self.assertEqual(self.app.summary()["applied"], 1)
        # Re-planning later doesn't bring back anything applied or rejected.
        self.process(rebuild=True, photo_ids=["1"])
        self.assertEqual(self.store.queue_item("1")["status"], "none")

    def test_reject_all_and_no_overwrite_of_flickr_edits(self):
        self.decide("3", [{"approve": False} for _ in self.store.queue_item("3")["changes"]])
        self.assertEqual(self.store.queue_item("3")["status"], "rejected")
        # Photo 5's description gets written on Flickr after the queue was built.
        self.fl.photos[4]["description"] = {"_content": "Mine"}
        r = self.decide("5", [{"approve": True} for _ in self.store.queue_item("5")["changes"]])
        self.assertEqual(self.fl.photos[4]["description"]["_content"], "Mine")      # re-read before setMeta
        desc = next(o for o in r["changes"] if o["action"] == "set_description")
        self.assertEqual(desc["status"], "rejected")
        self.assertEqual([j["photo_id"] for j in self.app.jobs_status()["finished"]], ["3", "5"])

    def test_process_unprocessed_video_from_viewer(self):
        video = photo("6", 35.0, 135.7, title="Ramen in Kyoto")
        video["media"] = "video"
        self.fl.photos.append(video)
        sync(self.fl, self.store, log=lambda *a: None)
        app = ReviewApp(self.store, self.fl, cfg=self.cfg, provider=lambda: self.vision,
                        process_kw={"geocoder": fake_geocoder, "downloader": lambda url: (b"jpg", "image/jpeg")})
        self.assertEqual(app.queue("unprocessed"), ["6"])
        self.assertEqual(app.item("6")["status"], "unprocessed")
        self.assertEqual(app.find("https://www.flickr.com/photos/someone/6/in/album-1"), "6")
        self.assertIsNone(app.find("https://www.flickr.com/photos/someone/999"))
        self.assertEqual(app.process("6"), {"status": "processing"})
        app.wait()
        item = app.item("6")
        self.assertEqual(item["status"], "pending")                     # classified despite include_videos: false
        self.assertIn(("add_to_album", "Food"), {(c["action"], c["new"]) for c in item["changes"] if c["action"] != "add_tags"})
        self.assertEqual(app.recent[-1]["kind"], "process")
        self.assertEqual(app.summary()["unprocessed"], 0)
        # Re-processing an already-queued photo works too, and changes nothing that's settled.
        app.process("6"); app.wait()
        self.assertEqual(app.item("6")["status"], "pending")

    def test_rebuild_videos_only_and_never_while_applying(self):
        self.fl.photos[0]["media"] = "video"                           # photo 1 becomes a video
        sync(self.fl, self.store, log=lambda *a: None)
        self.store.queue_finish("2", "applying", {"choices": []})      # mid-apply in the viewer
        self.cfg["vision"]["include_videos"] = True
        before = self.store.queue_item("3")["built_at"]
        stats = self.process(rebuild=True, media="video")
        self.assertEqual(stats["queued"] + stats["nothing_to_change"], 1)   # only the video
        self.assertEqual(self.store.queue_item("3")["built_at"], before)
        self.assertEqual(self.process(rebuild=True)["queued"] + self.process(rebuild=True)["nothing_to_change"], 4)
        self.assertEqual(self.store.queue_item("2")["status"], "applying")  # left alone

    def test_rows_and_choices_from_before_change_still_work(self):
        # A queue row and in-flight choices in the format used before Change existed.
        old_rows = [
            {"action": "add_tags", "photo_id": "1", "target": "food | ramen | noodle soup", "source": "vision:tags",
             "approve": "y", "original_target": "food | ramen | noodle soup"},
            {"action": "set_description", "photo_id": "1", "target": "A steaming bowl of ramen.",
             "source": "vision:describe", "approve": "y", "original_target": "A steaming bowl of ramen."},
        ]
        self.store.queue_put("1", "pending", old_rows)
        item = self.app.item("1")
        self.assertEqual(item["changes"][0]["new"], ["food", "ramen", "noodle soup"])
        self.assertEqual(item["changes"][1]["old"], "")      # it stored the new text there when blank
        r = self.decide("1", [{"approve": True, "target": "food | ramen"}, {"approve": True}])
        self.assertEqual(r["status"], "applied")
        self.assertNotIn("noodle", self.fl.photos[0]["tags"])
        self.assertEqual(self.fl.photos[0]["description"]["_content"], "A steaming bowl of ramen.")

    def test_unfinished_jobs_resume_on_restart(self):
        # Choices recorded, tool closed before the worker got to it.
        quiet = ReviewApp(self.store, self.fl, background=False)
        self.store.queue_finish("1", "applying", {"choices": [{"approve": True}] * len(self.store.queue_item("1")["changes"])})
        self.assertEqual(quiet.summary()["applying"], 1)
        ReviewApp(self.store, self.fl).wait()                          # a new session picks it up
        self.assertEqual(self.store.queue_item("1")["status"], "applied")
        self.assertIn("1", self.fl.sets["102"]["photos"])

    def test_queue_view_lanes_and_results(self):
        view = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg)
        for job in (("apply", "1"), ("process", "2"), ("sync", "")):
            view.jobs.put(job)
        view.running = {"kind": "process", "photo_id": "3", "started": 1.0}
        q = view.queue_view()
        self.assertEqual(q["waiting"], 3)
        self.assertEqual([(j["kind"], j["position"]) for j in q["flickr"]["jobs"]], [("apply", 1), ("sync", 3)])
        self.assertEqual([(j["photo_id"], j["position"]) for j in q["vision"]["jobs"]], [("2", 2)])
        self.assertEqual(q["flickr"]["jobs"][0]["title"], "Ramen in Tokyo")
        self.assertTrue(q["flickr"]["jobs"][0]["thumb"].endswith("_q.jpg"))
        self.assertEqual((q["running"]["photo_id"], q["vision"]["model"]), ("3", "m1"))
        # A real job through the background worker: timed, and listed under recent.
        choices = [{"approve": True} for _ in self.store.queue_item("1")["changes"]]
        self.decide("1", choices)
        done = self.app.queue_view()["recent"][0]
        self.assertEqual((done["kind"], done["photo_id"], done["status"]), ("apply", "1", "applied"))
        self.assertGreaterEqual(done["seconds"], 0)
        self.assertIsNone(self.app.queue_view()["running"])

    def test_processing_counts_vision_tokens(self):
        app = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg, provider=lambda: self.vision,
                        process_kw={"geocoder": fake_geocoder, "downloader": lambda url: (b"jpg", "image/jpeg")})
        self.store.db.execute("DELETE FROM classifications")
        self.store.db.commit()
        app.process("1")
        self.assertEqual(app.vision_usage["photos"], 1)
        self.assertGreater(app.vision_usage["input_tokens"], 0)


class NewPhotosTest(unittest.TestCase):
    """After a sync, photos uploaded since go under To review: processed now, or when opened."""

    def setUp(self):
        self.cfg = from_dict(CFG)
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.sqlite"))
        self.fl = FakeFlickr()
        self.app = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg, provider=lambda: FakeVision("m1"),
                             process_kw={"geocoder": fake_geocoder, "downloader": lambda url: (b"jpg", "image/jpeg")})
        self.app._sync(self.store, "full")              # the first sync: the whole library

    def upload(self, pid, at):
        self.fl.photos.append({**photo(pid, title="IMG_9"), "dateupload": str(at)})
        return self.app._sync(self.store, "full")

    def test_the_first_sync_queues_nothing(self):
        self.assertEqual(self.store.queue_counts(), {})
        self.assertEqual(self.app.summary()["unprocessed"], 5)          # the backlog stays under Not processed

    def test_queue_mode_processes_each_when_opened(self):
        self.assertEqual(self.upload("9", 1700000500)["new_photos"], 1)
        self.assertEqual(self.store.queue_item("9")["status"], "new")
        self.assertEqual(self.app.summary()["to_review"], 1)
        self.assertEqual(self.app.queue("pending"), ["9"])
        self.assertEqual(self.app.process("1", if_new=True), {"status": "unprocessed"})   # not new: left alone
        self.assertEqual(self.app.process("9", if_new=True)["status"], "pending")
        actions = {c["action"] for c in self.store.queue_item("9")["changes"]}
        self.assertTrue({"set_title", "set_description"} <= actions)
        self.assertEqual(self.upload("10", 1700000400)["new_photos"], 0)  # older than the last new one

    def test_process_mode_processes_them_after_the_sync(self):
        self.cfg["schedule"]["new_photos"] = "process"
        self.upload("9", 1700000500)
        self.assertEqual(self.store.queue_item("9")["status"], "pending")
        self.assertEqual(self.app.queue("pending"), ["9"])

    def test_without_a_vision_key_they_wait(self):
        self.cfg["schedule"]["new_photos"] = "process"

        def no_key():
            raise SystemExit("Missing the vision API key (vision.api_key), needed for provider 'claude'.")
        self.app._provider_factory = no_key
        self.upload("9", 1700000500)
        self.assertEqual(self.store.queue_item("9")["status"], "new")
        with self.assertRaises(RuntimeError):                            # not SystemExit: the worker survives
            self.app._process(self.store, "9")


if __name__ == "__main__":
    unittest.main()
