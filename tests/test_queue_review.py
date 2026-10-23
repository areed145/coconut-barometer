"""The review queue (process) and the review tool: a photo is in review from being sent there
until it's applied or ignored; re-processing keeps it there."""
import os
import tempfile
import time
import unittest

from fakes import photo, FakeFlickr, fake_geocoder, FakeVision, CFG
from coconut_barometer.config import from_dict
from coconut_barometer.pipeline import process
from coconut_barometer.review import ReviewApp
from coconut_barometer.store import Store
from coconut_barometer.sync import sync


class QueueReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = from_dict({**CFG, "vision": {**CFG["vision"], "describe": {"enabled": True}}})
        self.store = Store(os.path.join(self.tmp, "db.duckdb"))
        self.fl = FakeFlickr()
        sync(self.fl, self.store, log=lambda *a: None)
        self.vision = FakeVision("m1")
        self.stats = self.process()
        self.app = ReviewApp(self.store, self.fl)

    def decide(self, pid, choices):
        """Apply returns at once; the worker applies in the background. Applied: out of review.
        Still failing after the retry: back in review, with why."""
        self.assertEqual(self.app.decide(pid, choices), {"status": "applying"})
        self.app.wait()
        item = self.store.queue_item(pid)
        return {"status": "applied"} if item is None else {"status": item["status"], **item["result"]}

    def process(self, **kw):
        return process(self.cfg, self.store, self.fl, self.vision, geocoder=fake_geocoder, log=lambda *a: None,
                       downloader=lambda url: (b"jpg", "image/jpeg"), **kw)

    def changes(self, pid):
        return {(c["action"], tuple(c["new"]) if isinstance(c["new"], list) else c["new"])
                for c in self.store.queue_item(pid)["changes"]}

    def test_processing_skips_photos_already_processed_even_once_out_of_review(self):
        self.app.ignore("1")
        self.decide("2", [{"approve": True} for _ in self.store.queue_item("2")["changes"]])
        self.assertEqual(self.process()["queued"] + self.process()["nothing_to_change"], 0)   # not back in review
        self.assertEqual(self.process(photo_ids=["1"])["queued"], 1)                         # unless asked for

    def test_queue_is_built_per_photo_and_resumable(self):
        self.assertEqual(self.stats["queued"] + self.stats["nothing_to_change"], 5)
        self.assertEqual(self.store.camera_ids(), {"1", "2", "3", "4", "5"})      # EXIF fetched for each
        self.assertIn(("add_to_album", "Food"), self.changes("1"))
        self.assertIn(("set_description", "A steaming bowl of ramen."), self.changes("1"))
        self.assertEqual(self.process()["queued"], 0)                            # nothing re-done
        item = self.app.item("1")
        self.assertEqual(item["photo"]["title"], "Ramen in Tokyo")
        self.assertTrue(item["photo"]["image"].endswith("1_s_b.jpg"))

    def test_apply_writes_what_you_keep_and_leaves_review(self):
        changes = self.store.queue_item("1")["changes"]
        choices = []
        for c in changes:
            if c["action"] == "set_tags":
                choices.append({"approve": True, "value": ["food", "ramen"]})        # dropped "noodle soup"
            elif c["action"] == "set_description":
                choices.append({"approve": True, "value": "Tonkotsu ramen."})
            elif c["new"] == "Japan":
                choices.append({"approve": False})
            else:
                choices.append({"approve": True})
        before = self.app.summary()["to_review"]
        self.assertEqual(self.decide("1", choices)["status"], "applied")
        self.assertIsNone(self.store.queue_item("1"))                              # out of review
        self.assertEqual(self.app.summary()["to_review"], before - 1)
        self.assertIn("1", self.fl.sets["102"]["photos"])                          # Food
        self.assertNotIn("1", self.fl.sets["100"]["photos"])                       # Japan: unticked
        self.assertIn("ramen", self.fl.photos[0]["tags"])
        self.assertNotIn("noodle", self.fl.photos[0]["tags"])
        self.assertEqual(self.fl.photos[0]["description"]["_content"], "Tonkotsu ramen.")
        log = self.store.decisions()                                               # for Analytics
        self.assertEqual((log[("1", "add_tag", "ramen")], log[("1", "add_to_album", "Food")]), ("applied", "applied"))
        self.assertNotIn(("1", "add_tag", "noodle soup"), log)                     # unticked: nothing recorded
        # No memory: sent again, it's proposed afresh: Japan again, but not what's on Flickr now (Food).
        self.process(photo_ids=["1"])
        again = self.changes("1")
        self.assertIn(("add_to_album", "Japan"), again)
        self.assertNotIn(("add_to_album", "Food"), again)

    def test_ignore_takes_it_out_of_review_and_changes_nothing(self):
        calls = len(self.fl.calls)
        self.assertEqual(self.app.ignore("3"), {"status": "ignored"})
        self.assertIsNone(self.store.queue_item("3"))
        self.assertEqual(len(self.fl.calls), calls)                                # nothing sent to Flickr
        self.assertEqual(self.app.item("3")["status"], "not_queued")
        with self.assertRaises(KeyError):
            self.app.ignore("3")
        self.store.queue_finish("2", "applying", {"choices": []})
        self.assertEqual(self.app.ignore("2"), {"status": "busy"})                 # mid-apply: left alone
        # Nothing remembered: sent again, the same proposals.
        self.process(photo_ids=["3"])
        self.assertEqual(self.store.queue_item("3")["status"], "pending")

    def test_flickr_edits_are_never_overwritten_and_the_photo_stays_in_review(self):
        # Photo 5's description gets written on Flickr after it was proposed.
        self.fl.photos[4]["description"] = {"_content": "Mine"}
        r = self.decide("5", [{"approve": True} for _ in self.store.queue_item("5")["changes"]])
        self.assertEqual(self.fl.photos[4]["description"]["_content"], "Mine")      # re-read before setMeta
        self.assertEqual(r["status"], "pending")                                    # still in review...
        self.assertIn("changed on Flickr since proposed", r["error"])               # ...saying why
        self.assertEqual([c["action"] for c in self.store.queue_item("5")["changes"]], ["set_description"])
        self.assertEqual([j["photo_id"] for j in self.app.jobs_status()["finished"]], ["5"])

    def test_tags_kept_from_both_parts_in_one_update(self):
        self.fl.photos[0]["tags"] = "oldtag mine"
        sync(self.fl, self.store, log=lambda *a: None)
        self.process(photo_ids=["1"])
        changes = self.store.queue_item("1")["changes"]
        i = next(k for k, c in enumerate(changes) if c["action"] == "set_tags")
        choices = [{"approve": False} for _ in changes]
        choices[i] = {"approve": True, "value": ["food", "ramen", "mine"]}        # proposed, less one; and "mine" kept
        calls = len(self.fl.calls)
        self.assertEqual(self.decide("1", choices)["status"], "applied")
        self.assertEqual(self.fl.photos[0]["tags"], "food ramen mine")
        self.assertEqual([m for m, _ in self.fl.calls[calls:]].count("flickr.photos.setTags"), 1)

    def test_tags_already_as_chosen_make_no_call(self):
        self.fl.photos[0]["tags"] = "oldtag mine"
        sync(self.fl, self.store, log=lambda *a: None)
        self.process(photo_ids=["1"])
        changes = self.store.queue_item("1")["changes"]
        i = next(k for k, c in enumerate(changes) if c["action"] == "set_tags")
        choices = [{"approve": False} for _ in changes]
        choices[i] = {"approve": True, "value": ["mine", "oldtag"]}              # proposed off, current kept: as it is
        calls = len(self.fl.calls)
        self.decide("1", choices)
        self.assertNotIn("flickr.photos.setTags", [m for m, _ in self.fl.calls[calls:]])

    def test_queued_counts_every_job_running_or_waiting(self):
        app = ReviewApp(self.store, self.fl, background=False)          # no worker to take the jobs meanwhile
        self.assertEqual(app.jobs_status()["queued"], 0)                 # the Queue tab's dot: green
        app.jobs.put(("wx", ""))
        app.running = {"kind": "sync", "photo_id": "", "started": 0}
        self.assertEqual(app.jobs_status()["queued"], 2)                 # amber

    def test_a_failed_apply_is_retried_once(self):
        self.fl.fail_next["flickr.photosets.addPhoto"] = 1                         # a blip: the retry goes through
        self.assertEqual(self.decide("1", [{"approve": True} for _ in self.store.queue_item("1")["changes"]])["status"],
                         "applied")
        self.assertIn("1", self.fl.sets["102"]["photos"])
        self.fl.fail_next["flickr.photosets.addPhoto"] = 99                        # down: fails twice, stays in review
        r = self.decide("2", [{"approve": True} for _ in self.store.queue_item("2")["changes"]])
        self.assertEqual(r["status"], "pending")
        self.assertIn("Service currently unavailable", r["error"])
        left = self.store.queue_item("2")["changes"]
        self.assertEqual({c["action"] for c in left}, {"add_to_album"})              # only what didn't go through
        self.assertEqual(self.app.jobs_status()["finished"][-1]["status"], "failed")
        # Applying again later (Flickr back up) finishes it.
        self.fl.fail_next.clear()
        self.assertEqual(self.decide("2", [{"approve": True} for _ in left])["status"], "applied")

    def test_process_unprocessed_video_from_viewer(self):
        video = photo("6", 35.0, 135.7, title="Ramen in Kyoto")
        video["media"] = "video"
        self.fl.photos.append(video)
        sync(self.fl, self.store, log=lambda *a: None)
        app = ReviewApp(self.store, self.fl, cfg=self.cfg, provider=lambda: self.vision,
                        process_kw={"geocoder": fake_geocoder, "downloader": lambda url: (b"jpg", "image/jpeg")})
        self.assertNotIn("6", app.queue())
        self.assertEqual(app.item("6")["status"], "not_queued")
        self.assertEqual(app.find("https://www.flickr.com/photos/someone/6/in/album-1"), "6")
        self.assertIsNone(app.find("https://www.flickr.com/photos/someone/999"))
        self.assertEqual(app.send("6"), {"status": "processing"})                   # sent: processed at once
        app.wait()
        item = app.item("6")
        self.assertEqual(item["status"], "pending")                     # classified despite include_videos: false
        self.assertIn(("add_to_album", "Food"), {(c["action"], c["new"]) for c in item["changes"] if c["action"] not in ("add_tags", "set_tags")})
        self.assertEqual(app.recent[-1]["kind"], "process")
        self.assertIn("6", app.queue())
        self.assertEqual(app.send("6"), {"status": "pending"})                      # already in review
        # Re-processing keeps it in review.
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
        self.assertIsNone(self.store.queue_item("1"))                  # applied: out of review
        self.assertIn("1", self.fl.sets["102"]["photos"])

    def test_queue_view_lanes_and_results(self):
        view = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg)
        for job in (("apply", "1"), ("process", "2"), ("sync", "")):
            view.jobs.put(job)
        view.running = {"kind": "process", "photo_id": "3", "started": 1.0}
        q = view.queue_view()
        self.assertEqual((q["waiting"], q["by_kind"]), (3, {"apply": 1, "process": 1, "sync": 1}))
        self.assertEqual([(j["kind"], j["photo_id"], j["position"]) for j in q["line"]],
                         [("apply", "1", 1), ("process", "2", 2), ("sync", None, 3)])
        self.assertEqual(q["line"][0]["title"], "Ramen in Tokyo")
        self.assertTrue(q["line"][0]["thumb"].endswith("_q.jpg"))
        self.assertEqual((q["running"]["photo_id"], q["services"]["vision"]["model"]), ("3", "m1"))
        # No job has finished yet, so there's nothing to estimate from.
        self.assertEqual(([j["starts_in"] for j in q["line"]], q["done_in"]), ([None] * 3, None))
        # Once each kind has taken 10 s, the line is timed from that, behind what's left of the running job.
        view.recent.extend({"kind": k, "seconds": 10.0} for k in ("apply", "process", "sync"))
        view.running["started"] = time.time() - 4
        q = view.queue_view()
        self.assertEqual(([j["starts_in"] for j in q["line"]], q["done_in"]), ([6, 16, 26], 36))
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

    def test_tags_are_proposed_as_the_whole_list(self):
        self.fl.photos[0]["tags"] = "oldtag ramen"
        sync(self.fl, self.store, log=lambda *a: None)
        app = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg, provider=lambda: self.vision,
                        process_kw={"geocoder": fake_geocoder, "downloader": lambda url: (b"jpg", "image/jpeg")})
        app.process("1")
        changes = self.store.queue_item("1")["changes"]
        tags = next(c for c in changes if c["action"] == "set_tags")
        self.assertNotIn("add_tags", {c["action"] for c in changes})
        self.assertEqual(tags["old"], "oldtag ramen")
        self.assertIn("ramen", tags["new"])                            # wanted and already there: kept
        self.assertNotIn("oldtag", tags["new"])
        choices = []
        for c in changes:
            if c["action"] == "set_tags":
                choices.append({"approve": True, "value": [t for t in c["new"] if t != "noodle soup"]})
            else:
                choices.append({"approve": False})
        out = app.decide("1", choices)
        self.assertEqual(out["status"], "applied")
        on_flickr = self.fl.photos[0]["tags"].split()
        self.assertNotIn("oldtag", on_flickr)
        self.assertIn("ramen", on_flickr)
        self.assertNotIn("noodlesoup", on_flickr)                     # dropped in review
        d = self.store.decisions()
        self.assertEqual(d[("1", "remove_tag", "oldtag")], "applied")
        self.assertNotIn(("1", "add_tag", "noodle soup"), d)
        self.assertEqual(self.store.photo("1")["tags"].split(), on_flickr)

    def test_overwrite_is_skipped_if_tags_changed_on_flickr(self):
        app = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg, provider=lambda: self.vision,
                        process_kw={"geocoder": fake_geocoder, "downloader": lambda url: (b"jpg", "image/jpeg")})
        app.process("1")
        changes = self.store.queue_item("1")["changes"]
        self.fl.photos[0]["tags"] = "addedbyhand"                      # on Flickr, after the proposal
        out = app.decide("1", [{"approve": c["action"] == "set_tags"} for c in changes])
        self.assertEqual(self.fl.photos[0]["tags"], "addedbyhand")     # left alone
        self.assertEqual(out["status"], "failed")                      # and it stays in review, saying why
        self.assertIn("tags changed on Flickr", self.store.queue_item("1")["result"]["error"])

    def test_overwrite_never_wipes_a_photo_with_no_wanted_tags(self):
        from coconut_barometer.planner import build_plan
        self.fl.photos[3]["tags"] = "mine"
        sync(self.fl, self.store, log=lambda *a: None)
        rows = build_plan(self.cfg, self.store, None, geocoder=fake_geocoder, photo_ids={"4"})
        self.assertEqual([r for r in rows if r.action == "set_tags"], [])


class NewPhotosTest(unittest.TestCase):
    """After a sync, photos uploaded since go into review: processed now, or when opened (review.when_queued)."""

    def setUp(self):
        self.cfg = from_dict({**CFG, "review": {"missing_location": False}})   # (MissingLocationTest has those)
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))
        self.fl = FakeFlickr()
        self.app = ReviewApp(self.store, self.fl, background=False, cfg=self.cfg, provider=lambda: FakeVision("m1"),
                             process_kw={"geocoder": fake_geocoder, "downloader": lambda url: (b"jpg", "image/jpeg")})
        self.app._sync(self.store, "full")              # the first sync: the whole library

    def upload(self, pid, at):
        self.fl.photos.append({**photo(pid, title="IMG_9"), "dateupload": str(at)})
        return self.app._sync(self.store, "full")

    def test_the_first_sync_queues_nothing(self):
        self.assertEqual(self.store.queue_counts(), {})                 # the backlog: Photos → Not processed
        self.assertEqual(self.app.summary()["to_review"], 0)

    def test_queue_mode_processes_each_when_opened(self):
        self.cfg["review"]["when_queued"] = "queue"
        self.assertEqual(self.upload("9", 1700000500)["new_photos"], 1)
        self.assertEqual(self.store.queue_item("9")["status"], "new")
        self.assertEqual(self.app.summary()["to_review"], 1)
        self.assertEqual(self.app.queue(), ["9"])
        self.assertEqual(self.app.process("1", if_new=True), {"status": "not_queued"})   # not in review: left alone
        self.assertEqual(self.app.send("1"), {"status": "new"})        # from the gallery: waits too
        self.assertEqual(self.app.process("9", if_new=True)["status"], "pending")
        actions = {c["action"] for c in self.store.queue_item("9")["changes"]}
        self.assertTrue({"set_title", "set_description"} <= actions)
        self.assertEqual(self.upload("10", 1700000400)["new_photos"], 0)  # older than the last new one

    def test_process_mode_processes_them_after_the_sync(self):
        self.assertEqual(self.cfg["review"]["when_queued"], "process")   # the default
        self.upload("9", 1700000500)
        self.assertEqual(self.store.queue_item("9")["status"], "pending")
        self.assertEqual(self.app.queue(), ["9"])
        self.assertEqual(self.app.send("1")["status"], "pending")      # from the gallery: processed too

    def test_waiting_photos_are_processed_on_startup(self):
        self.store.queue_add("2")                                      # left waiting (say, queue mode until now)
        app = ReviewApp(self.store, self.fl, cfg=self.cfg, provider=lambda: FakeVision("m1"),
                        process_kw={"geocoder": fake_geocoder, "downloader": lambda url: (b"jpg", "image/jpeg")})
        app.wait()
        self.assertEqual(self.store.queue_item("2")["status"], "pending")

    def test_without_a_vision_key_they_wait(self):

        def no_key():
            raise SystemExit("Missing the vision API key (vision.api_key), needed for provider 'claude'.")
        self.app._provider_factory = no_key
        self.upload("9", 1700000500)
        self.assertEqual(self.store.queue_item("9")["status"], "new")
        with self.assertRaises(RuntimeError):                            # not SystemExit: the worker survives
            self.app._process(self.store, "9")


if __name__ == "__main__":
    unittest.main()
