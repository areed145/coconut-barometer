"""The one-time import of the former SQLite database into DuckDB: offline (`migrate-sqlite`), and in the
running app (setup page / admin API), including what happens when it fails."""
import gzip
import hashlib
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

import duckdb
from fastapi.testclient import TestClient

from fakes import CFG, FakeFlickr
from flickr_organizer import backup as B
from flickr_organizer.cli import main as cli_main
from flickr_organizer.config import from_dict
from flickr_organizer.store import SCHEMA_VERSION, Store
from flickr_organizer.store.migration import Migration, find_sqlite_source, import_sqlite, is_sqlite_file, read_status
from flickr_organizer.web.app import create_app
from flickr_organizer.web.security import hash_password

PASSWORD = "correct horse battery"
NONCE, CIPHER = bytes(range(12)), bytes(range(255, 200, -1))


def make_sqlite(path, extra_photo_cols=False, bad_row=False):
    """A SQLite library in the shape of an early release (no zoom URLs, no stats tables): the columns it
    lacks come in as NULL, the tables it lacks stay empty."""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE photos (id TEXT PRIMARY KEY, title TEXT, description TEXT, tags TEXT, lat REAL, lon REAL,
            accuracy INTEGER, media TEXT, thumb_url TEXT, secret TEXT, server TEXT, date_taken TEXT,
            date_upload INTEGER, last_update INTEGER, ispublic INTEGER, views INTEGER, faves INTEGER, comments INTEGER);
        CREATE TABLE albums (id TEXT PRIMARY KEY, title TEXT, count INTEGER);
        CREATE TABLE memberships (album_id TEXT, photo_id TEXT, PRIMARY KEY (album_id, photo_id));
        CREATE TABLE users (username TEXT PRIMARY KEY, password_hash TEXT, created_at INTEGER, updated_at INTEGER);
        CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, username TEXT, csrf TEXT, created_at INTEGER, expires_at INTEGER,
            last_seen INTEGER, ip TEXT, user_agent TEXT);
        CREATE TABLE settings (path TEXT PRIMARY KEY, value TEXT, updated_at INTEGER, updated_by TEXT);
        CREATE TABLE secrets (name TEXT PRIMARY KEY, nonce BLOB, ciphertext BLOB, hint TEXT, updated_at INTEGER, updated_by TEXT);
        CREATE TABLE settings_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER, username TEXT, path TEXT, old TEXT, new TEXT);
    """)
    db.execute("INSERT INTO meta VALUES ('schema_version', '2'), ('nsid', '1@N00')")
    db.executemany("INSERT INTO photos (id, title, description, tags, lat, lon, date_upload, views) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   [("1", "Ramen's \"best\"", "line1\nline2 — ünïcode ✓", "ramen tokyo", 35.689487, 139.691711, 1700000000, 12),
                    ("2", "Untitled", None, "", None, None, 1700000100, 0)])
    if bad_row:
        db.execute("INSERT INTO photos (id, title, date_upload) VALUES ('3', 'bad', 'not a number')")
    db.execute("INSERT INTO albums VALUES ('a1', 'Japan', 1)")
    db.execute("INSERT INTO memberships VALUES ('a1', '1')")
    db.execute("INSERT INTO users VALUES ('me', ?, 1, 1)", (hash_password(PASSWORD),))
    db.execute("INSERT INTO sessions VALUES ('tokhash', 'me', 'csrf', 1, ?, 1, '1.2.3.4', 'ua')", (int(time.time()) + 3600,))
    db.execute("INSERT INTO settings VALUES ('web.sync_every_minutes', '15', 1, 'me')")
    db.execute("INSERT INTO secrets VALUES ('vision.api_key', ?, ?, '1234', 1, 'me')", (NONCE, CIPHER))
    db.executemany("INSERT INTO settings_log (at, username, path, old, new) VALUES (?, 'me', 'p', 'a', 'b')", [(1,), (2,), (3,)])
    db.commit()
    db.close()


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src, self.dst = os.path.join(self.tmp, "lib.db"), os.path.join(self.tmp, "lib.duckdb")
        make_sqlite(self.src)

    def test_everything_arrives_exactly_and_the_source_is_untouched(self):
        before = digest(self.src)
        counts = import_sqlite(self.src, self.dst)
        self.assertEqual((counts["photos"], counts["users"], counts["secrets"], counts["settings_log"]), (2, 1, 1, 3))
        self.assertEqual(digest(self.src), before)
        s = Store(self.dst)
        self.assertEqual(int(s.get_meta("schema_version")), SCHEMA_VERSION)       # the new database's own
        self.assertEqual(s.get_meta("nsid"), "1@N00")
        p = s.photo("1")
        self.assertEqual((p["title"], p["description"], p["lat"], p["lon"]),
                         ("Ramen's \"best\"", "line1\nline2 — ünïcode ✓", 35.689487, 139.691711))   # doubles stay doubles
        self.assertIsNone(p["url_l"])                                                 # a column the old file never had
        self.assertIsNone(s.photo("2")["lat"])
        row = s.secret_rows()["vision.api_key"]
        self.assertEqual((bytes(row["nonce"]), bytes(row["ciphertext"])), (NONCE, CIPHER))   # binary values intact
        self.assertEqual(s.usernames(), ["me"])
        self.assertEqual(s.memberships(), {"1": {"a1"}})
        s.close()

    def test_new_settings_log_rows_continue_after_the_imported_ids(self):
        import_sqlite(self.src, self.dst)
        s = Store(self.dst)
        s.db.execute('INSERT INTO settings_log ("at", username, path, old, new) VALUES (5, \'me\', \'p\', \'a\', \'b\')')
        self.assertEqual(sorted(r[0] for r in s.db.execute("SELECT id FROM settings_log")), [1, 2, 3, 4])
        s.close()

    def test_a_failed_import_leaves_nothing_behind(self):
        bad = os.path.join(self.tmp, "bad.db")
        make_sqlite(bad, bad_row=True)                       # a text value in an integer column
        with self.assertRaises(Exception):
            import_sqlite(bad, self.dst)
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith("lib.duckdb")], [])
        notours = os.path.join(self.tmp, "x.db")
        sqlite3.connect(notours).execute("CREATE TABLE t (x)").connection.commit()
        with self.assertRaisesRegex(ValueError, "no photos table"):
            import_sqlite(notours, self.dst)
        with self.assertRaises(FileNotFoundError):
            import_sqlite(os.path.join(self.tmp, "missing.db"), self.dst)

    def test_it_will_not_overwrite_an_existing_database(self):
        import_sqlite(self.src, self.dst)
        with self.assertRaises(FileExistsError):
            import_sqlite(self.src, self.dst)

    def test_the_command_line(self):
        cli_main(["migrate-sqlite", self.src, self.dst])
        self.assertEqual(duckdb.connect(self.dst, read_only=True).execute("SELECT COUNT(*) FROM photos").fetchone()[0], 2)

    def test_a_sqlite_file_is_not_opened_as_the_database(self):
        self.assertTrue(is_sqlite_file(self.src))
        with self.assertRaisesRegex(ValueError, "SQLite database"):
            Store(self.src)

    def test_finding_the_source_beside_the_database(self):
        self.assertEqual(find_sqlite_source(self.dst), __import__("pathlib").Path(self.src))
        self.assertIsNone(find_sqlite_source(os.path.join(self.tmp, "other.duckdb")))
        self.assertEqual(find_sqlite_source(os.path.join(self.tmp, "other.duckdb"), self.src).name, "lib.db")

    def test_restoring_a_sqlite_backup_converts_it(self):
        gz = os.path.join(self.tmp, "flickr_organizer-20200101-000000.db.gz")
        with open(self.src, "rb") as raw, gzip.open(gz, "wb") as f:
            f.write(raw.read())
        info = B.check_backup_file(gz)
        self.assertEqual(info["keys"], 1)
        cfg = from_dict({"database": self.dst})
        B.restore(cfg, gz)
        s = Store(self.dst)
        self.assertEqual((s.usernames(), s.photo("1")["title"]), (["me"], "Ramen's \"best\""))
        s.close()


class AppMigrationTest(unittest.TestCase):
    """A fresh DuckDB beside the old SQLite file, as on Render after the first deploy of this version."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "flickr_organizer.db")
        self.dst = os.path.join(self.tmp, "flickr_organizer.duckdb")
        make_sqlite(self.src)
        self.cfg = from_dict({**CFG, "database": self.dst, "web": {"allowed_hosts": ["testserver"]}})
        self.app = create_app(self.cfg, None, scheduler=False, background=False)
        self.rt = self.app.state.runtime
        self.c = TestClient(self.app, base_url="https://testserver")

    def finish(self):
        self.rt.migration.wait(30)
        return self.c.get("/api/migration/status").json()

    def test_the_setup_page_offers_it_and_the_code_protects_it(self):
        self.assertEqual(self.c.get("/api/setup").json(), {"needed": True, "sqlite_found": True})
        self.assertEqual(self.c.get("/api/migration/status").json()["state"], "not_started")
        self.assertEqual(self.c.post("/api/setup/migrate", json={"code": "wrong"}).status_code, 403)
        self.assertEqual(self.c.get("/api/migration/status").json()["state"], "not_started")

    def test_import_from_setup_then_sign_in_with_the_old_login(self):
        r = self.c.post("/api/setup/migrate", json={"code": self.rt.setup_code})
        self.assertEqual((r.status_code, r.json()["state"]), (200, "running"))
        done = self.finish()
        self.assertEqual((done["state"], done["counts"]["photos"], done["tables_done"] == done["tables_total"]), ("complete", 2, True))
        self.assertEqual(done["rows_done"], done["rows_total"])
        self.assertNotIn("source", done)                       # no paths in a status anyone can read
        self.assertEqual(self.c.get("/api/setup").json()["needed"], False)             # the old users came with it
        login = self.c.post("/api/auth/login", json={"username": "me", "password": PASSWORD})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(self.c.get("/api/photos").json()["total"], 2)
        self.assertEqual(self.c.post("/api/setup/migrate", json={"code": "x"}).status_code, 409)   # setup is over
        self.assertTrue(is_sqlite_file(self.src))                                      # the SQLite file is still there

    def test_the_status_survives_a_restart_and_a_second_import_is_refused(self):
        self.c.post("/api/setup/migrate", json={"code": self.rt.setup_code})
        self.finish()
        self.assertEqual(Migration(self.dst, None).view()["state"], "complete")
        self.c.post("/api/auth/login", json={"username": "me", "password": PASSWORD})
        csrf = {"X-CSRF-Token": self.c.get("/api/auth/session").json()["csrf"]}
        r = self.c.post("/api/admin/database/migration", json={"password": PASSWORD}, headers=csrf)
        self.assertEqual(r.status_code, 409)

    def test_a_failure_changes_nothing_and_can_be_retried(self):
        bad = os.path.join(self.tmp, "bad.db")
        make_sqlite(bad, bad_row=True)
        os.replace(bad, self.src)
        self.c.post("/api/setup/migrate", json={"code": self.rt.setup_code})
        failed = self.finish()
        self.assertEqual(failed["state"], "failed")
        self.assertIn("Conversion", failed["error"])             # which value, and why, is in the status
        s = Store(self.dst)
        self.assertEqual(s.db.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 0)    # rolled back, not half-copied
        self.assertEqual(s.usernames(), [])
        s.close()
        self.assertEqual(self.c.get("/api/setup").json()["needed"], True)
        make_sqlite(self.src + ".fixed")
        os.replace(self.src + ".fixed", self.src)
        self.assertEqual(self.c.post("/api/setup/migrate", json={"code": self.rt.setup_code}).status_code, 200)
        self.assertEqual(self.finish()["state"], "complete")

    def test_a_server_that_stopped_mid_import_reports_it_as_failed(self):
        from flickr_organizer.store.migration import write_status
        write_status(self.dst, {"state": "running", "started_at": 1})
        self.assertEqual(Migration(self.dst, None).view()["state"], "failed")

    def test_the_admin_api_replaces_the_login_made_at_setup(self):
        made = self.c.post("/api/setup/user", json={"code": self.rt.setup_code, "username": "temp", "password": "another long password"})
        self.assertEqual(made.status_code, 200)
        csrf = {"X-CSRF-Token": made.json()["csrf"]}
        self.assertEqual(self.c.post("/api/admin/database/migration", json={"password": "wrong password!"}, headers=csrf).status_code, 403)
        r = self.c.post("/api/admin/database/migration", json={"password": "another long password"}, headers=csrf)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.finish()["state"], "complete")
        self.assertEqual(Store(self.dst).usernames(), ["me"])

    def test_it_only_fills_a_database_with_no_photos(self):
        s = Store(self.dst)
        s.db.execute("INSERT INTO photos (id, title) VALUES ('9', 'mine')")
        s.close()
        r = self.c.post("/api/setup/migrate", json={"code": self.rt.setup_code})
        self.assertEqual((r.status_code, "already has photos" in r.json()["detail"]), (409, True))

    def test_background_jobs_wait_while_it_runs_instead_of_writing_into_the_tables_being_replaced(self):
        """A scheduled sync wrote meta.nsid mid-import and the copy then failed on commit with a duplicate key."""
        from flickr_organizer.store import migration as M
        app = create_app(self.cfg, FakeFlickr(), scheduler=False, background=True)
        rt = app.state.runtime
        c = TestClient(app, base_url="https://testserver")
        started, release, real = threading.Event(), threading.Event(), M.copy_tables

        def slow(source, con, progress=lambda u: None):
            started.set()
            release.wait(20)
            return real(source, con, progress)

        with mock.patch.object(M, "copy_tables", slow):
            self.assertEqual(c.post("/api/setup/migrate", json={"code": rt.setup_code}).status_code, 200)
            self.assertTrue(started.wait(10))
            self.assertEqual(rt.review.sync(), {"status": "importing"})              # the scheduler's call is turned away
            self.assertEqual(rt.review.wx_sync()["status"], "no_token")
            rt.review.jobs.put(("sync", ""))                                         # one that was already queued
            time.sleep(1)
            self.assertIsNone(rt.review.running)                                     # ...does not start
            release.set()
            rt.migration.wait(30)
        self.assertEqual(c.get("/api/migration/status").json()["state"], "complete")
        rt.review.wait()

    def test_it_will_not_start_while_a_background_job_is_running(self):
        self.rt.review.running = {"kind": "sync", "photo_id": "", "started": time.time()}
        r = self.c.post("/api/setup/migrate", json={"code": self.rt.setup_code})
        self.assertEqual((r.status_code, "background job" in r.json()["detail"]), (409, True))
        self.rt.review.running = None
        self.assertEqual(self.c.post("/api/setup/migrate", json={"code": self.rt.setup_code}).status_code, 200)
        self.finish()

    def test_without_a_sqlite_file_there_is_nothing_to_offer(self):
        os.remove(self.src)
        app = create_app(from_dict({**CFG, "database": os.path.join(self.tmp, "fresh.duckdb"),
                                    "web": {"allowed_hosts": ["testserver"]}}), None, scheduler=False, background=False)
        c = TestClient(app, base_url="https://testserver")
        self.assertEqual(c.get("/api/setup").json(), {"needed": True, "sqlite_found": False})
        self.assertEqual(c.post("/api/setup/migrate", json={"code": app.state.runtime.setup_code}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
