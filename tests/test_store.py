"""The store: schema upgrades, and keeping changes applied during a sync."""
import os
import tempfile
import unittest

from fakes import FakeFlickr
from flickr_organizer.store import SCHEMA_VERSION, SchemaTooNew, Store
from flickr_organizer.sync import sync


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "db.duckdb")

    def test_fresh_database_is_at_the_latest_version(self):
        store = Store(self.path)
        self.assertEqual(int(store.get_meta("schema_version")), SCHEMA_VERSION)
        tables = {r[0] for r in store.db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'")}
        self.assertTrue({"photos", "queue", "decisions", "cameras", "faves", "flickr_groups"} <= tables)

    def test_refuses_a_database_from_a_newer_version(self):
        Store(self.path).set_meta("schema_version", SCHEMA_VERSION + 1)
        with self.assertRaises(SchemaTooNew):
            Store(self.path)


class ConcurrentWritersTest(unittest.TestCase):
    """DuckDB fails a second writer at once instead of waiting for the first (SQLite waited)."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        self.a = Store(self.path)

    def test_opening_another_connection_does_not_write(self):
        with self.a.db:                                   # a is busy rewriting the version row
            self.a.db.execute("UPDATE meta SET value = value WHERE key = 'schema_version'")
            Store(self.path).close()                      # a request thread opening its own connection meanwhile

    def test_a_single_statement_waits_its_turn_instead_of_failing(self):
        import threading
        import time
        self.a.set_meta("k", "1")
        held = threading.Event()

        def hold():
            with self.a.db:
                self.a.db.execute("UPDATE meta SET value = 'a' WHERE key = 'k'")
                held.set()
                time.sleep(0.4)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(5)
        b = Store(self.path)
        b.set_meta("k", "b")                              # the same row, while a's transaction is open
        t.join()
        self.assertEqual(b.get_meta("k"), "b")


class OneProcessTest(unittest.TestCase):
    def test_a_second_process_gets_a_clear_message_not_a_stack_trace(self):
        import subprocess
        import sys
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        store = Store(path)                                   # this process holds the file
        out = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); from flickr_organizer.store import Store; Store(%r)"
                              % (os.path.join(os.path.dirname(__file__), ".."), path)], capture_output=True, text=True)
        store.close()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("open in another process", out.stderr)
        self.assertNotIn("Traceback", out.stderr)


class SyncKeepsChangesTest(unittest.TestCase):
    def test_album_and_tag_added_mid_sync_survive(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))
        fl = FakeFlickr()
        sync(fl, store, log=lambda *a: None)
        real_call = fl.call

        def call(method, **p):   # the review tool applies while the sync's pages are in flight
            out = real_call(method, **p)
            if method == "flickr.people.getPhotos" and int(p.get("page", 1)) == 1:
                store.record("1", "add_to_album", "Food", "applied")    # photo 1 wasn't in Food before
                store.record("1", "add_tag", "midsync", "applied")
            return out
        fl.call = call
        sync(fl, store, log=lambda *a: None)
        self.assertIn("102", store.memberships().get("1", set()))
        self.assertIn("midsync", store.photo("1")["tags"].split())


if __name__ == "__main__":
    unittest.main()
