"""The rate limit shared across processes using the same Flickr API key."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from coconut_barometer.ratelimit import STALE_AFTER, SharedRateLimiter, fcntl, limiter_path

ROOT = os.path.join(os.path.dirname(__file__), "..")
INTERVAL = 0.05


def gaps(times):
    times = sorted(times)
    return [b - a for a, b in zip(times, times[1:])]


@unittest.skipUnless(fcntl, "shared limiting needs fcntl (not on Windows)")
class SharedRateLimiterTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "rate.lock")

    def test_two_limiters_on_one_file_share_the_rate(self):
        times = []

        def caller():   # two separate limiters, as two commands would have
            lim = SharedRateLimiter(self.path, 1 / INTERVAL)
            for _ in range(6):
                lim.wait()
                times.append(time.time())
        threads = [threading.Thread(target=caller) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(times), 12)
        self.assertGreaterEqual(min(gaps(times)), INTERVAL * 0.8)       # never two at once
        self.assertGreaterEqual(max(times) - min(times), INTERVAL * 11 * 0.9)

    def test_separate_processes_share_the_rate(self):
        script = ("import sys, time; sys.path.insert(0, sys.argv[1]);"
                  "from coconut_barometer.ratelimit import SharedRateLimiter;"
                  "lim = SharedRateLimiter(sys.argv[2], 10)\n"
                  "for _ in range(5):\n    lim.wait(); print(time.time(), flush=True)")
        procs = [subprocess.Popen([sys.executable, "-c", script, ROOT, self.path], stdout=subprocess.PIPE, text=True)
                 for _ in range(2)]
        times = [float(line) for p in procs for line in p.communicate(timeout=30)[0].split()]
        self.assertEqual(len(times), 10)
        self.assertGreaterEqual(min(gaps(times)), 0.1 * 0.8)            # 10/s across both processes

    def test_bad_or_stale_reservations_are_reset(self):
        for content in ("not a number", repr(time.time() + STALE_AFTER * 2)):
            with open(self.path, "w") as f:
                f.write(content)
            self.assertLess(SharedRateLimiter(self.path, 1).wait(), 0.5)

    def test_one_file_per_key_and_the_key_isnt_in_it(self):
        a, b = limiter_path("key-one", "/tmp/x"), limiter_path("key-two", "/tmp/x")
        self.assertNotEqual(a, b)
        self.assertNotIn("key-one", a)

    def test_zero_rate_means_no_limit(self):
        self.assertEqual(SharedRateLimiter(self.path, 0).wait(), 0.0)


if __name__ == "__main__":
    unittest.main()
