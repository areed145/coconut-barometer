"""The memory watchdog (memory.py, #197): the limit and resident size read, tracing from 60%, memory given back and a
report from 80% (at most hourly), a summary saved so a restart after high memory is reported, and heavy() one at a
time."""
import json
import os
import tempfile
import threading
import time
import tracemalloc
import unittest

from coconut_barometer import memory

MB = 1024 * 1024
GB = 1024 * MB


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def __call__(self):
        return self.t


class LimitAndRss(unittest.TestCase):
    def test_limit_from_env_then_cgroup(self):
        files = {"/sys/fs/cgroup/memory.max": "2147483648"}
        self.assertEqual(memory.limit_bytes({"MEMORY_LIMIT_MB": "512"}, files.get), 512 * MB)
        self.assertEqual(memory.limit_bytes({}, files.get), 2 * GB)
        self.assertEqual(memory.limit_bytes({}, {"/sys/fs/cgroup/memory.max": "max"}.get), None)
        v1 = {"/sys/fs/cgroup/memory/memory.limit_in_bytes": str(1 << 62)}     # v1's "no limit"
        self.assertEqual(memory.limit_bytes({}, v1.get), None)
        self.assertEqual(memory.limit_bytes({}, {"/sys/fs/cgroup/memory/memory.limit_in_bytes": "1073741824"}.get), GB)

    def test_rss_from_statm(self):
        page = os.sysconf("SC_PAGE_SIZE")
        self.assertEqual(memory.rss_bytes({"/proc/self/statm": "9999 1000 50 1 0 200 0"}.get), 1000 * page)
        self.assertIsNone(memory.rss_bytes({}.get))


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.rss = 100 * MB
        self.reports, self.relieved = [], 0
        self.clock = Clock()
        self.dir = tempfile.mkdtemp()

        def relieve():
            self.relieved += 1
        self.w = memory.Watchdog(rss=lambda: self.rss, limit=lambda: GB, clock=self.clock,
                                 report=lambda e, where: self.reports.append((e, where)), relieve_fn=relieve)
        self.w.limit = GB
        self.w.state_path = os.path.join(self.dir, memory.STATE_FILE)
        self.w.describe("Worker", lambda: "sync running, 3 waiting")
        self.addCleanup(lambda: tracemalloc.is_tracing() and tracemalloc.stop())

    def test_quiet_under_the_watch_mark(self):
        self.assertEqual(self.w.sample(), "ok")
        self.assertFalse(tracemalloc.is_tracing())
        self.assertEqual((self.reports, self.relieved), ([], 0))
        self.assertFalse(os.path.exists(self.w.state_path))

    def test_traces_and_saves_from_the_watch_mark_and_stops_tracing_when_back_down(self):
        self.rss = 650 * MB
        self.assertEqual(self.w.sample(), "watch")
        self.assertTrue(tracemalloc.is_tracing())
        saved = json.load(open(self.w.state_path))
        self.assertEqual(saved["rss"], 650 * MB)
        self.assertEqual(saved["extra"]["Worker"], "sync running, 3 waiting")
        self.assertEqual(self.reports, [])
        self.rss = 500 * MB                                   # under 60%, over 45%: still tracing
        self.w.sample()
        self.assertTrue(tracemalloc.is_tracing())
        self.rss = 300 * MB
        self.w.sample()
        self.assertFalse(tracemalloc.is_tracing())

    def test_gives_memory_back_first_and_reports_only_if_still_high(self):
        self.rss = 850 * MB

        def relieve():
            self.relieved += 1
            self.rss = 700 * MB                               # gc + malloc_trim brought it under 80%
        self.w._relieve = relieve
        self.assertEqual(self.w.sample(), "watch")
        self.assertEqual((self.relieved, self.reports), (1, []))

    def test_reports_at_the_soft_mark_at_most_hourly(self):
        self.rss = 870 * MB
        self.assertEqual(self.w.sample(), "soft")
        self.assertEqual(len(self.reports), 1)
        exc, where = self.reports[0]
        self.assertIsInstance(exc, memory.MemoryPressure)
        self.assertEqual(exc._coconut_fingerprint, "memory-pressure")
        self.assertEqual(where, "memory watchdog")
        text = str(exc)
        self.assertTrue(text.startswith("Memory at 870 MB of the 1,024 MB limit (85%)"))
        self.assertIn("Worker: sync running, 3 waiting", text)
        self.assertIn("Biggest allocations since tracing started", text)
        self.clock.t += 600
        self.w.sample()
        self.assertEqual(len(self.reports), 1)
        self.clock.t += memory.REPORT_EVERY
        self.rss = 950 * MB
        self.assertEqual(self.w.sample(), "hard")
        self.assertEqual(len(self.reports), 2)
        self.assertEqual(self.w.status()["level"], "hard")
        self.assertEqual(self.w.status()["peak"], 950 * MB)

    def test_a_restart_after_high_memory_is_reported_once(self):
        json.dump({"at": self.clock.t, "rss": 1000 * MB, "limit": GB, "peak": 1000 * MB, "top": [
            {"where": "coconut_barometer/weather/radar.py:89", "size": 300 * MB, "count": 3}], "threads": ["radar"],
            "extra": {}}, open(self.w.state_path, "w"))
        self.assertTrue(self.w.check_restart())
        exc = self.reports[0][0]
        self.assertEqual(exc._coconut_fingerprint, "memory-restart")
        self.assertIn("Restarted after memory reached 1,000 MB of 1,024 MB", str(exc))
        self.assertIn("coconut_barometer/weather/radar.py:89", str(exc))
        self.assertFalse(os.path.exists(self.w.state_path))
        self.assertFalse(self.w.check_restart())

    def test_a_restart_from_moderate_memory_or_a_clean_stop_isnt_reported(self):
        json.dump({"at": self.clock.t, "rss": 650 * MB, "limit": GB}, open(self.w.state_path, "w"))
        self.assertFalse(self.w.check_restart())
        self.assertFalse(os.path.exists(self.w.state_path))
        json.dump({"at": self.clock.t, "rss": 1000 * MB, "limit": GB}, open(self.w.state_path, "w"))
        self.w.stopped()
        self.assertFalse(self.w.check_restart())
        self.assertEqual(self.reports, [])

    def test_unreadable_rss_isnt_watched(self):
        w = memory.Watchdog(rss=lambda: None, limit=lambda: GB)
        w.start(threading.Event(), self.dir)
        self.assertFalse(w.status()["watched"])
        self.assertEqual(w.sample(), "ok")


class HeavyTest(unittest.TestCase):
    def test_one_at_a_time(self):
        inside, most = [0], [0]
        lock = threading.Lock()

        def work():
            with memory.heavy("test"):
                with lock:
                    inside[0] += 1
                    most[0] = max(most[0], inside[0])
                time.sleep(0.02)
                with lock:
                    inside[0] -= 1
        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(most[0], 1)

    def test_names_what_runs_and_releases_on_error(self):
        with memory.heavy("radar frame"):
            self.assertEqual(memory.watchdog.summary()["heavy"], ["radar frame"])
        with self.assertRaises(ValueError):
            with memory.heavy("boom"):
                raise ValueError
        self.assertEqual(memory._heavy_what, [])
        self.assertTrue(memory._heavy.acquire(blocking=False))
        memory._heavy.release()


if __name__ == "__main__":
    unittest.main()
