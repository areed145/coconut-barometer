"""The activity log behind the Queue page: calls waiting for a rate-limit slot, calls made, stream
messages, finished jobs; and that the clients and streams write to it."""
import os
import tempfile
import threading
import time
import unittest

from fakes import FakeFlickr, CFG
from coconut_barometer import activity
from coconut_barometer.activity import Activity
from coconut_barometer.aprs.stream import Hub, handle
from coconut_barometer.config import from_dict
from coconut_barometer.flickr_client import FlickrClient
from coconut_barometer.ratelimit import SharedRateLimiter
from coconut_barometer.review import ReviewApp
from coconut_barometer.store import Store


class ActivityTest(unittest.TestCase):
    def test_a_call_waits_for_its_slot_then_runs_and_is_logged(self):
        a = Activity()
        t = a.begin("flickr", "flickr.photos.getInfo", "photo_id=1")
        a.until(t, time.time() + 60)
        self.assertEqual([(p["state"], p["what"]) for p in a.snapshot()["pending"]], [("waiting", "flickr.photos.getInfo")])
        a.running(t)
        self.assertEqual(a.snapshot()["pending"][0]["state"], "running")
        a.end(t)
        (e,) = a.since(0)
        self.assertEqual((e["source"], e["status"], e["error"], e["detail"]), ("flickr", "ok", False, "photo_id=1"))
        s = a.snapshot()
        self.assertEqual((s["pending"], s["sources"]["flickr"]["calls"], s["sources"]["flickr"]["last_minute"]), ([], 1, 1))

    def test_errors_are_logged_and_raised(self):
        a = Activity()
        with self.assertRaises(ValueError):
            with a.call("vision", "claude:m"):
                raise ValueError("bad output")
        (e,) = a.since(0)
        self.assertEqual((e["status"], e["error"], e["detail"]), ("error", True, "ValueError: bad output"))
        self.assertEqual(a.snapshot()["sources"]["vision"]["errors"], 1)

    def test_a_chatty_source_doesnt_push_out_the_others(self):
        a = Activity(keep=5)
        a.record("jobs", "Flickr sync", kind="job")
        for i in range(50):
            a.record("aprs", "position", str(i))
        a.skip("aprs")
        got = a.since(0)
        self.assertEqual([e["source"] for e in got].count("jobs"), 1)
        self.assertEqual([e["detail"] for e in got if e["source"] == "aprs"], ["45", "46", "47", "48", "49"])
        self.assertEqual(a.since(got[-2]["seq"]), [got[-1]])
        self.assertEqual(a.snapshot()["sources"]["aprs"]["skipped"], 1)


class FakeApi:
    class photos:
        @staticmethod
        def getInfo(**params):
            return {"photo": params}


class InstrumentedTest(unittest.TestCase):
    def test_flickr_calls_record_the_wait_for_the_shared_limit(self):
        limiter = SharedRateLimiter(os.path.join(tempfile.mkdtemp(), "rate.lock"), 4)    # 0.25 s apart
        client = FlickrClient("k", "s", limiter=limiter)
        client.api = FakeApi()
        seq = activity.log.since(0)[-1]["seq"] if activity.log.since(0) else 0
        seen = []
        watch = threading.Thread(target=lambda: [seen.extend(p["state"] for p in activity.log.snapshot()["pending"]) or time.sleep(0.02)
                                                 for _ in range(30)])
        watch.start()
        for pid in ("1", "2", "3"):
            client.call("flickr.photos.getInfo", photo_id=pid, secret="never-logged")
        watch.join()
        got = [e for e in activity.log.since(seq) if e["source"] == "flickr"]
        self.assertEqual([e["detail"] for e in got], ["photo_id=1", "photo_id=2", "photo_id=3"])
        self.assertGreater(got[-1]["waited"], 0.1)
        self.assertIn("waiting", seen)
        self.assertGreater(limiter.next_free(), time.time())

    def test_aprs_lines_are_logged_and_keepalives_counted(self):
        store, seq = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb")), activity.log.snapshot()
        skipped = seq["sources"].get("aprs", {}).get("skipped", 0)
        last = activity.log.since(0)[-1]["seq"] if activity.log.since(0) else 0
        for line in ["# aprsc keepalive", "# logresp KJ4TFD-RO unverified, server T2X",
                     "KJ4TFD-10>APRS:!3016.20N/09744.40W>090/025", "W1AW>APRS:>hello", "no header at all"]:
            handle(line, store, Hub(), ["KJ4TFD-10"])
        got = [(e["what"], e["status"], e["error"]) for e in activity.log.since(last) if e["source"] == "aprs"]
        self.assertEqual(got, [("login", "receive-only", False), ("position", "stored", False),
                               ("status", "not a tracked position", False), ("unreadable packet", "ParseError", True)])
        self.assertEqual(activity.log.snapshot()["sources"]["aprs"]["skipped"], skipped + 1)

    def test_finished_jobs_are_logged_with_their_calls(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))
        app = ReviewApp(store, FakeFlickr(), cfg=from_dict(CFG))
        last = activity.log.since(0)[-1]["seq"] if activity.log.since(0) else 0
        app.sync()
        app.wait()
        (job,) = [e for e in activity.log.since(last) if e["source"] == "jobs"]
        self.assertEqual((job["what"], job["kind"], job["status"]), ("Flickr sync", "job", "done"))
        self.assertIn("Flickr calls", job["detail"])


if __name__ == "__main__":
    unittest.main()
