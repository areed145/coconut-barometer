"""The job lanes (jobs.py, #197): a lane per service working side by side, each line by priority then first come,
Flickr's express worker for what you asked for, one job per resource at a time, aging, and the running job's priority
for the rate limiter (which keeps part of Flickr's hour for calls someone's waiting on)."""
import os
import tempfile
import threading
import time
import unittest

from coconut_barometer import jobs
from coconut_barometer.jobs import FILL, INTERACTIVE, SCHEDULED, USER, AGE_EVERY, Lanes
from coconut_barometer.ratelimit import SharedRateLimiter, fcntl


def worker(lanes, name):
    return next(w for w in lanes.workers if w.name == name)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class LanesTest(unittest.TestCase):
    def test_kinds_go_to_their_service_lanes(self):
        q = Lanes()
        for item in [("sync", ""), ("process", "1"), ("wx", ""), ("music", ""), ("musicbrainz", ""), ("profiles", "")]:
            q.put(item)
        self.assertEqual({(j.kind, j.lane) for j in q.waiting()},
                         {("sync", "flickr"), ("process", "vision"), ("wx", "weather"), ("music", "music"),
                          ("musicbrainz", "musicbrainz"), ("profiles", "music")})

    def test_each_line_by_priority_then_first_come(self):
        q = Lanes()
        q.put(("musicbrainz", ""))                      # FILL
        q.put(("process", "a"))                         # SCHEDULED (arrived)
        q.put(("music", ""))                            # INTERACTIVE
        q.put(("process", "b"), USER)                   # you asked
        self.assertEqual(q.queue, [("process", "b"), ("music", ""), ("process", "a"), ("musicbrainz", "")])

    def test_a_waiting_job_gains_a_level_each_age_every_but_not_past_interactive(self):
        clock = Clock()
        q = Lanes(clock)
        q.put(("profiles", ""))                         # FILL, waiting a while
        clock.t += AGE_EVERY
        q.put(("music", ""), SCHEDULED)
        self.assertEqual(q.queue, [("profiles", ""), ("music", "")])     # aged to SCHEDULED, and asked first
        clock.t += 10 * AGE_EVERY
        q.put(("music", "x"), USER)
        job = next(j for j in q.waiting() if j.kind == "profiles")
        self.assertEqual(job.effective(clock()), INTERACTIVE)
        self.assertEqual(q.queue[0], ("music", "x"))                      # never ahead of what you asked for

    def test_a_long_job_in_one_lane_doesnt_hold_up_another(self):
        q = Lanes()
        release, ran = threading.Event(), []

        def run(job):
            if job.kind == "sync":
                release.wait(5)
            ran.append(job.kind)
        q.start(run)
        q.put(("sync", ""))
        q.put(("music", ""))
        q.put(("musicbrainz", ""))
        deadline = time.time() + 5
        while len(ran) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(sorted(ran), ["music", "musicbrainz"])          # while the sync still runs
        release.set()
        q.join()
        self.assertEqual(sorted(ran), ["music", "musicbrainz", "sync"])

    def test_flickrs_express_worker_takes_only_what_you_asked_for(self):
        q = Lanes()
        main, express = worker(q, "flickr"), worker(q, "flickr-express")
        q.put(("sync", ""))
        self.assertIsNone(q.take(express, 0))                            # a scheduled sync: not for express
        self.assertEqual(q.take(main, 0).kind, "sync")                   # the main worker runs it...
        q.put(("apply", "1"))
        q.put(("sync", "full"))
        self.assertEqual(q.take(express, 0).item, ("apply", "1"))       # ...and an apply needn't wait for it
        self.assertEqual([j.worker for j in q.running()], ["flickr", "flickr-express"])

    def test_one_job_per_resource_across_lanes(self):
        q = Lanes()
        vision, flickr, express = worker(q, "vision"), worker(q, "flickr"), worker(q, "flickr-express")
        q.put(("process", "1"))
        self.assertEqual(q.take(vision, 0).item, ("process", "1"))
        q.put(("apply", "1"))
        q.put(("apply", "2"))
        self.assertEqual(q.take(express, 0).item, ("apply", "2"))       # photo 1 is busy in the vision lane
        self.assertIsNone(q.take(flickr, 0))
        q.done(vision)
        self.assertEqual(q.take(flickr, 0).item, ("apply", "1"))
        q.put(("sync", ""))
        q.put(("sync", "full"))
        q.done(flickr)
        q.done(express)
        self.assertEqual(q.take(flickr, 0).item, ("sync", ""))
        self.assertIsNone(q.take(express, 0))                            # one sync at a time (and it's scheduled)

    def test_has_counts_running_and_waiting(self):
        q = Lanes()
        q.put(("wx", ""))
        self.assertTrue(q.has("wx"))
        q.take(worker(q, "weather"), 0)
        self.assertTrue(q.has("wx"))
        q.done(worker(q, "weather"))
        self.assertFalse(q.has("wx"))

    def test_the_worker_runs_each_job_under_its_priority(self):
        q, seen = Lanes(), {}
        q.start(lambda job: seen.setdefault(job.kind, (jobs.current_priority(), jobs.background())))
        q.put(("musicbrainz", ""))
        q.put(("apply", "1"))
        q.join()
        self.assertEqual(seen, {"musicbrainz": (FILL, True), "apply": (USER, False)})
        self.assertEqual((jobs.current_priority(), jobs.background()), (INTERACTIVE, False))   # a request's thread


@unittest.skipUnless(fcntl, "shared limiting needs fcntl (not on Windows)")
class ReserveTest(unittest.TestCase):
    def test_background_calls_leave_the_reserve_for_ones_someone_waits_on(self):
        path = os.path.join(tempfile.mkdtemp(), "rate.lock")
        lim = SharedRateLimiter(path, 10, 0, window=30, reserve=0.2)       # 8 for background, all 10 otherwise
        self.assertEqual(lim.background_window, 8)
        with jobs.priority(SCHEDULED):
            waits = [lim.wait() for _ in range(8)]
            self.assertTrue(all(w < 0.5 for w in waits))
            with open(path) as f:
                slots = lim._prune(lim._parse(f.read()), time.time())
            self.assertGreater(lim._next_slot(slots, time.time(), True), time.time() + 20)   # a 9th waits for the window
        waits = [lim.wait() for _ in range(2)]                           # a request's calls still go at once
        self.assertTrue(all(w < 0.5 for w in waits))

    def test_no_reserve_by_default(self):
        lim = SharedRateLimiter(os.path.join(tempfile.mkdtemp(), "rate.lock"), 10, 0)
        self.assertEqual(lim.background_window, 10)


if __name__ == "__main__":
    unittest.main()
