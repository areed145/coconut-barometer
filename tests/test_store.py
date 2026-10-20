"""The store: schema upgrades, and keeping changes applied during a sync."""
import os
import sqlite3
import tempfile
import unittest

from fakes import FakeFlickr
from flickr_organizer.store import SCHEMA_VERSION, Store
from flickr_organizer.sync import sync


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "db.sqlite")

    def test_fresh_database_is_at_the_latest_version(self):
        store = Store(self.path)
        self.assertEqual(int(store.get_meta("schema_version")), SCHEMA_VERSION)
        tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"photos", "queue", "decisions", "cameras", "faves", "flickr_groups"} <= tables)

    def test_database_from_before_versioning_is_upgraded_in_place(self):
        old = sqlite3.connect(self.path)       # the shape of the very first release
        old.executescript("""
            CREATE TABLE photos (id TEXT PRIMARY KEY, title TEXT, description TEXT, tags TEXT,
                lat REAL, lon REAL, accuracy INTEGER, media TEXT, thumb_url TEXT, secret TEXT, server TEXT,
                date_taken TEXT, date_upload INTEGER, last_update INTEGER, ispublic INTEGER);
            CREATE TABLE cameras (photo_id TEXT PRIMARY KEY, camera TEXT, make TEXT, model TEXT, fetched_at INTEGER);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO photos (id, title) VALUES ('1', 'Kept');
        """)
        old.commit()
        old.close()
        store = Store(self.path)
        self.assertEqual(int(store.get_meta("schema_version")), SCHEMA_VERSION)
        cols = {r[1] for r in store.db.execute("PRAGMA table_info(photos)")}
        self.assertTrue({"views", "faves", "date_taken_unknown", "width"} <= cols)
        self.assertIn("exif", {r[1] for r in store.db.execute("PRAGMA table_info(cameras)")})
        self.assertEqual(store.photo("1")["title"], "Kept")
        Store(self.path)                       # opening again changes nothing

    def test_refuses_a_database_from_a_newer_version(self):
        Store(self.path).set_meta("schema_version", SCHEMA_VERSION + 1)
        with self.assertRaises(SystemExit):
            Store(self.path)


class SyncKeepsChangesTest(unittest.TestCase):
    def test_album_and_tag_added_mid_sync_survive(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "db.sqlite"))
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
