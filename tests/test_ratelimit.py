"""The rate limit shared across processes using the same Flickr API key."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from coconut_barometer.ratelimit import STALE_AFTER, SharedRateLimiter, estimate_seconds, fcntl, limiter_path

ROOT = os.path.join(os.path.dirname(__file__), "..")
INTERVAL = 0.05
WINDOW = 0.3
EPS = 1e-6       # float rounding only: slots are handed out under the file lock, so their spacing is exact
EARLY = 1e-3     # sleep runs on the monotonic clock, slots on the wall clock: allow them to differ this much


def gaps(times):
    times = sorted(times)
    return [b - a for a, b in zip(times, times[1:])]


@unittest.skipUnless(fcntl, "shared limiting needs fcntl (not on Windows)")
class SharedRateLimiterTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "rate.lock")

    # These check the slots handed out, not when each thread happened to wake: the scheduler can run a
    # thread a little late and the next one on time, which made wall-clock gaps flaky.
    def test_two_limiters_on_one_file_share_the_rate(self):
        slots, early = [], []

        def caller():   # two separate limiters, as two commands would have
            lim = SharedRateLimiter(self.path, 0, 1 / INTERVAL)
            for _ in range(6):
                mine = []
                lim.wait(on_slot=mine.append)
                slots.append(mine[0])
                if time.time() < mine[0] - EARLY:
                    early.append(mine[0])
        threads = [threading.Thread(target=caller) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(slots), 12)
        self.assertGreaterEqual(min(gaps(slots)), INTERVAL - EPS)       # never two at once
        self.assertEqual(early, [])                                     # nobody went before their slot

    def test_separate_processes_share_the_rate(self):
        script = ("import sys, time; sys.path.insert(0, sys.argv[1]);"
                  "from coconut_barometer.ratelimit import SharedRateLimiter;"
                  "lim = SharedRateLimiter(sys.argv[2], 0, 10)\n"
                  "for _ in range(5):\n    s = []; lim.wait(on_slot=s.append); print(s[0], time.time() >= s[0] - 1e-3, flush=True)")
        procs = [subprocess.Popen([sys.executable, "-c", script, ROOT, self.path], stdout=subprocess.PIPE, text=True)
                 for _ in range(2)]
        rows = [line.split() for p in procs for line in p.communicate(timeout=30)[0].splitlines()]
        self.assertEqual(len(rows), 10)
        self.assertGreaterEqual(min(gaps([float(s) for s, _ in rows])), 0.1 - EPS)   # 10/s across both processes
        self.assertEqual({on_time for _, on_time in rows}, {"True"})

    def test_bad_or_stale_reservations_are_reset(self):
        for content in ("not a number", repr(time.time() + STALE_AFTER * 2)):
            with open(self.path, "w") as f:
                f.write(content)
            self.assertLess(SharedRateLimiter(self.path, 3600, 1).wait(), 0.5)

    def test_one_file_per_key_and_the_key_isnt_in_it(self):
        a, b = limiter_path("key-one", "/tmp/x"), limiter_path("key-two", "/tmp/x")
        self.assertNotEqual(a, b)
        self.assertNotIn("key-one", a)

    def test_zero_rate_means_no_limit(self):
        self.assertEqual(SharedRateLimiter(self.path, 0, 0).wait(), 0.0)

    # The hourly budget, with the hour shrunk to WINDOW seconds.
    def test_calls_go_at_once_while_the_window_has_room(self):
        lim = SharedRateLimiter(self.path, 5, 0, window=WINDOW)
        self.assertEqual([lim.wait() for _ in range(5)], [0.0] * 5)   # not spaced a second apart

    def test_a_full_window_waits_for_the_oldest_call_to_age_out(self):
        slots = []
        lim = SharedRateLimiter(self.path, 3, 0, window=WINDOW)
        for _ in range(7):
            lim.wait(on_slot=slots.append)
        for i in range(3, 7):
            self.assertGreaterEqual(slots[i] - slots[i - 3], WINDOW - EPS)   # never 4 in one window
        self.assertLess(slots[3] - slots[0], WINDOW + 0.1)                   # but no later than it must

    def test_two_limiters_on_one_file_share_the_hour(self):
        slots = []

        def caller():
            lim = SharedRateLimiter(self.path, 4, 0, window=WINDOW)
            for _ in range(3):
                lim.wait(on_slot=slots.append)
        threads = [threading.Thread(target=caller) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        slots.sort()
        self.assertEqual(len(slots), 6)
        self.assertGreaterEqual(slots[4] - slots[0], WINDOW - EPS)
        self.assertGreaterEqual(slots[5] - slots[1], WINDOW - EPS)

    def test_the_burst_cap_still_spaces_calls(self):
        slots = []
        lim = SharedRateLimiter(self.path, 100, 1 / INTERVAL, window=WINDOW)
        for _ in range(4):
            lim.wait(on_slot=slots.append)
        self.assertGreaterEqual(min(gaps(slots)), INTERVAL - EPS)

    def test_next_free_reports_a_full_window_without_reserving(self):
        lim = SharedRateLimiter(self.path, 2, 0, window=60)
        lim.wait(), lim.wait()
        self.assertGreater(lim.next_free(), time.time() + 50)
        self.assertGreater(lim.next_free(), time.time() + 50)   # asking twice doesn't push it further
        self.assertLess(SharedRateLimiter(self.path, 3, 0, window=60).next_free(), time.time() + 1)

    def test_an_old_single_reservation_file_still_reads(self):
        with open(self.path, "w") as f:   # the format before the hourly window
            f.write(repr(time.time() - 1))
        self.assertEqual(SharedRateLimiter(self.path, 3600, 1).wait(), 0.0)


class EstimateTest(unittest.TestCase):
    def test_estimates(self):
        self.assertEqual(estimate_seconds(100, 3600, 5), 20)               # all inside the hour: full speed
        self.assertEqual(estimate_seconds(3600 + 1800, 3600, 5), 1800)     # the rest at the hourly rate
        self.assertEqual(estimate_seconds(10, 0, 0), 0)


if __name__ == "__main__":
    unittest.main()
