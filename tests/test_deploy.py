"""Running on a server (Render): platform settings, the data directory, key formats,
backups, the health check, and connecting Flickr from the admin page."""
import base64
import gzip
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from fakes import FakeFlickr, CFG
from flickr_organizer import backup as B
from flickr_organizer.config import apply_env, database_path, from_dict, read_raw
from flickr_organizer.settings import _key_from_text
from flickr_organizer.store import Store
from flickr_organizer.sync import sync
from flickr_organizer.web.app import create_app
from flickr_organizer.web.security import hash_password

PASSWORD = "correct horse battery"


class PlatformTest(unittest.TestCase):
    def test_render_listens_on_its_port_behind_its_proxy(self):
        env = {"RENDER": "true", "PORT": "10000", "RENDER_EXTERNAL_HOSTNAME": "photos.onrender.com",
               "FLICKR_ORGANIZER_ALLOWED_HOSTS": "photos.example.com, www.example.com"}
        web = apply_env({"web": {"allowed_hosts": ["localhost"], "secure_cookies": False}}, env)["web"]
        self.assertEqual((web["host"], web["port"], web["trusted_proxies"], web["secure_cookies"]),
                         ("0.0.0.0", 10000, ["*"], True))
        self.assertEqual(web["allowed_hosts"], ["localhost", "photos.example.com", "www.example.com", "photos.onrender.com"])
        self.assertNotIn("web", apply_env({}, {}))                     # nothing set: nothing added

    def test_config_is_optional_with_a_data_directory(self):
        data = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"FLICKR_ORGANIZER_DATA": data}):
            self.assertEqual(read_raw(os.path.join(data, "no-such-config.yaml")), {})
            self.assertEqual(database_path("/elsewhere/config.yaml", {}), os.path.join(data, "flickr_organizer.duckdb"))
        with self.assertRaises(SystemExit):
            read_raw(os.path.join(data, "no-such-config.yaml"))           # locally it's still required

    def test_encryption_key_formats(self):
        raw = os.urandom(32)
        self.assertEqual(_key_from_text(base64.b64encode(raw).decode()), raw)          # Render's generateValue
        self.assertEqual(_key_from_text(base64.urlsafe_b64encode(raw).decode()), raw)  # `settings new-key`
        self.assertEqual(len(_key_from_text("x" * 40)), 32)                           # a long passphrase
        with self.assertRaises(SystemExit):
            _key_from_text("short")


class BackupTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.cfg = from_dict({"database": os.path.join(d, "db.duckdb"), "backup": {"keep": 2}})
        self.store = Store(self.cfg["database"])
        sync(FakeFlickr(), self.store, log=lambda *a: None)

    def test_backups_are_compressed_complete_and_pruned(self):
        names = []
        for _ in range(3):
            names.append(B.make_backup(self.cfg).name)
            time.sleep(1.1)                                       # names have one-second resolution
        kept = [b["name"] for b in B.list_backups(self.cfg)]
        self.assertEqual(kept, sorted(names, reverse=True)[:2])
        path = B.backup_path(self.cfg, kept[0])
        self.assertEqual(oct(path.stat().st_mode)[-3:], "600")
        plain = path.with_suffix("")
        plain.write_bytes(gzip.decompress(path.read_bytes()))
        import duckdb
        con = duckdb.connect(str(plain), read_only=True)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 5)
        con.close()

    def test_restore_keeps_the_old_database_whole_and_drops_its_write_ahead_log(self):
        backup = B.make_backup(self.cfg)
        self.store.set_meta("changed_after_backup", "yes")
        self.store.close()
        wal = self.cfg["database"] + ".wal"
        with open(wal, "wb") as f:
            f.write(b"left behind by the database being replaced")
        kept = B.restore(self.cfg, str(backup))
        self.assertFalse(os.path.exists(wal))
        restored = Store(self.cfg["database"])
        self.assertIsNone(restored.get_meta("changed_after_backup"))
        restored.close()
        self.assertGreater(kept.stat().st_size, 0)

    def test_names_outside_the_backup_folder_are_refused(self):
        for name in ("../db.duckdb", "/etc/passwd", "flickr_organizer-x.db.gz/../../db.duckdb", "other.db.gz"):
            with self.assertRaises(FileNotFoundError):
                B.backup_path(self.cfg, name)

    def test_restore_checks_the_file_and_keeps_the_current_database(self):
        backup = B.make_backup(self.cfg)
        self.store.delete_photos(["1", "2"])
        self.store.close()
        kept = B.restore(self.cfg, str(backup))
        self.assertEqual(len(Store(self.cfg["database"]).photos()), 5)
        self.assertEqual(len(Store(str(kept)).photos()), 3)
        bad = os.path.join(os.path.dirname(self.cfg["database"]), "bad.db")
        open(bad, "wb").write(b"not a database")
        with self.assertRaises(Exception):
            B.restore(self.cfg, bad)


class RestoreOnStartTest(unittest.TestCase):
    def test_a_dropped_in_backup_is_restored_once_before_start(self):
        from flickr_organizer.cli import _restore_on_start

        data = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"FLICKR_ORGANIZER_DATA": data}):
            cfg = from_dict({"database": os.path.join(data, "flickr_organizer.db")})
            store = Store(cfg["database"])
            sync(FakeFlickr(), store, log=lambda *a: None)
            os.replace(B.make_backup(cfg), os.path.join(data, "restore.db.gz"))
            store.delete_photos(["1", "2", "3"])
            store = _restore_on_start(cfg, store)
            self.assertEqual(len(store.photos()), 5)
            self.assertFalse(os.path.exists(os.path.join(data, "restore.db.gz")))     # not applied twice
            self.assertTrue(any(n.startswith("restored-") for n in os.listdir(data)))
            self.assertIs(_restore_on_start(cfg, store), store)                     # nothing to do now


class FakeFlickrAuth:
    """Just enough of flickrapi's OAuth for the admin-page login flow."""
    issued = 0

    def __init__(self, cfg):
        self.api = self
        self.token = None
        self.verified = None

    def get_request_token(self, oauth_callback):
        FakeFlickrAuth.issued += 1
        self.token, self.callback = f"req{FakeFlickrAuth.issued}", oauth_callback

    def auth_url(self, perms):
        return f"https://www.flickr.com/services/oauth/authorize?oauth_token={self.token}&perms={perms}"

    def get_access_token(self, verifier):
        self.verified = verifier

    def token_valid(self, perms):
        return False


class ServerTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        store = Store(path)
        sync(FakeFlickr(), store, log=lambda *a: None)
        store.set_password_hash("owner", hash_password(PASSWORD))
        self.made = []
        cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]},
                         "flickr": {"api_key": "test-key", "api_secret": "test-secret"}})
        self.app = create_app(cfg, FakeFlickrAuth(cfg), scheduler=False,
                              client_factory=lambda c: self.made.append(FakeFlickrAuth(c)) or self.made[-1])
        self.c = TestClient(self.app, base_url="https://testserver")
        self.h = {"X-CSRF-Token": self.c.post("/api/auth/login", json={"username": "owner", "password": PASSWORD}).json()["csrf"]}

    def test_health_check_needs_no_login(self):
        anon = TestClient(self.app, base_url="https://testserver")
        r = anon.get("/healthz")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))

    def test_connect_flickr_round_trip(self):
        url = self.c.post("/api/admin/flickr/connect", headers=self.h).json()["url"]
        self.assertIn("flickr.com/services/oauth/authorize", url)
        self.assertEqual(self.made[0].callback, "https://testserver/api/flickr/callback")
        token = url.split("oauth_token=")[1].split("&")[0]
        anon = TestClient(self.app, base_url="https://testserver", follow_redirects=False)   # no cookie on the way back
        self.assertEqual(anon.get("/api/flickr/callback", params={"oauth_token": "forged", "oauth_verifier": "x"}).status_code, 400)
        r = anon.get("/api/flickr/callback", params={"oauth_token": token, "oauth_verifier": "v123"})
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/#/settings"))
        self.assertEqual(self.made[0].verified, "v123")
        self.assertIs(self.app.state.runtime.client, self.made[-1])                       # rebuilt with the login
        self.assertEqual(anon.get("/api/flickr/callback", params={"oauth_token": token, "oauth_verifier": "v"}).status_code, 400)

    def test_expired_flickr_logins_are_refused(self):
        url = self.c.post("/api/admin/flickr/connect", headers=self.h).json()["url"]
        token = url.split("oauth_token=")[1].split("&")[0]
        rt = self.app.state.runtime
        client, started, user = rt.flickr_pending[token]
        rt.flickr_pending[token] = (client, started - rt.FLICKR_LOGIN_TTL - 1, user)
        self.assertEqual(self.c.get("/api/flickr/callback", params={"oauth_token": token, "oauth_verifier": "v"}).status_code, 400)

    def test_backups_api(self):
        self.assertEqual(self.c.post("/api/admin/backups", headers=self.h).status_code, 200)
        name = self.c.get("/api/admin/backups").json()["backups"][0]["name"]
        wrong = self.c.post(f"/api/admin/backups/{name}/download", json={"password": "nope"}, headers=self.h)
        self.assertEqual(wrong.status_code, 403)
        r = self.c.post(f"/api/admin/backups/{name}/download", json={"password": PASSWORD}, headers=self.h)
        self.assertEqual((r.status_code, r.content[:2]), (200, b"\x1f\x8b"))                # gzip
        self.assertEqual(self.c.post("/api/admin/backups/..%2Fdb.duckdb/download", json={"password": PASSWORD},
                                     headers=self.h).status_code, 404)

    def test_restore_upload_is_checked_then_staged(self):
        restarts = []
        self.app.state.runtime.restart_soon = lambda: restarts.append(1)
        folder = os.path.dirname(self.app.state.runtime.cfg["database"])
        backup = B.make_backup(self.app.state.runtime.cfg).read_bytes()
        up = lambda body, pw=PASSWORD: self.c.post("/api/admin/restore", content=body,
                                                    headers={**self.h, "X-Confirm-Password": pw})
        self.assertEqual(up(backup, "nope").status_code, 403)
        self.assertEqual(up(b"").status_code, 400)
        self.assertEqual(up(gzip.compress(b"not a database")).status_code, 422)
        other = os.path.join(tempfile.mkdtemp(), "other.db")
        sqlite3.connect(other).execute("CREATE TABLE t (x)").connection.commit()
        with open(other, "rb") as f:
            self.assertEqual(up(f.read()).status_code, 422)                              # a database, not ours
        self.assertFalse(any(n.startswith("restore") or n.endswith(".part") for n in os.listdir(folder)))
        r = up(backup)
        self.assertEqual((r.status_code, r.json()["staged"], restarts), (200, "restore.db.gz", [1]))
        self.assertEqual(oct(os.stat(os.path.join(folder, "restore.db.gz")).st_mode & 0o777), "0o600")
        self.assertEqual(self.c.post("/api/admin/restore", content=backup, headers={"X-Confirm-Password": PASSWORD})
                         .status_code, 403)                                               # no CSRF token


class SetupTest(unittest.TestCase):
    """No login yet: the setup page creates one or restores a backup, with the code from the log."""

    def setUp(self):
        self.data = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {"FLICKR_ORGANIZER_DATA": self.data})
        env.start()
        self.addCleanup(env.stop)
        for var in ("FLICKR_ORGANIZER_KEY_FILE", "FLICKR_ORGANIZER_SECRET_KEY"):
            os.environ.pop(var, None)
        self.cfg = from_dict({**CFG, "database": os.path.join(self.data, "flickr_organizer.duckdb"),
                              "web": {"allowed_hosts": ["testserver"]}})
        Store(self.cfg["database"]).close()
        self.app = create_app(self.cfg, None, scheduler=False, background=False)   # no worker holding the file open
        self.rt = self.app.state.runtime
        self.restarts = []
        self.rt.restart_soon = lambda: self.restarts.append(1)
        self.c = TestClient(self.app, base_url="https://testserver")

    def test_first_login_needs_the_code_and_only_works_once(self):
        self.assertEqual(self.c.get("/api/setup").json(), {"needed": True, "sqlite_found": False})
        user = {"username": "owner", "password": PASSWORD}
        self.assertEqual(self.c.post("/api/setup/user", json={**user, "code": "0000-0000-0000"}).status_code, 403)
        self.assertEqual(self.c.post("/api/setup/user", json={**user, "code": self.rt.setup_code,
                                                               "password": "short"}).status_code, 422)
        r = self.c.post("/api/setup/user", json={**user, "code": self.rt.setup_code.lower()})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get("/api/auth/session").json()["username"], "owner")      # signed in
        self.assertEqual(self.c.get("/api/setup").json(), {"needed": False, "sqlite_found": False})
        self.assertEqual(self.c.post("/api/setup/user", json={**user, "username": "intruder", "code": "x"}).status_code, 409)

    def test_wrong_codes_are_throttled(self):
        for _ in range(20):
            r = self.c.post("/api/setup/user", json={"username": "x", "password": PASSWORD, "code": "nope"})
        self.assertEqual(r.status_code, 429)

    def test_restore_from_another_machine_brings_its_key(self):
        from flickr_organizer import settings as S
        from flickr_organizer.cli import _restore_on_start

        laptop = tempfile.mkdtemp()                       # the laptop: a database with an API key in it
        laptop_key = S.new_key()
        with mock.patch.dict(os.environ, {"FLICKR_ORGANIZER_SECRET_KEY": laptop_key}):
            lcfg = from_dict({"database": os.path.join(laptop, "db.duckdb")})
            ldb = Store(lcfg["database"])
            sync(FakeFlickr(), ldb, log=lambda *a: None)
            ldb.set_password_hash("me", hash_password(PASSWORD))
            S.set_secret(ldb, "flickr.api_key", "laptop-flickr-key", "cli")
            backup = B.make_backup(lcfg).read_bytes()
        up = lambda key="", code=None: self.c.post("/api/setup/restore", content=backup, headers={
            "X-Setup-Code": code or self.rt.setup_code, "X-Encryption-Key": key})
        self.assertEqual(up(code="wrong").status_code, 403)
        self.assertEqual(up(S.new_key()).status_code, 422)                                  # not its key
        r = up(laptop_key)
        self.assertEqual((r.status_code, r.json()["keys"], r.json()["readable"], self.restarts), (200, 1, True, [1]))
        self.rt.store().close()                            # a restart closes every connection to the file
        self.rt.review.store.close()
        server = Store(self.cfg["database"])
        server = _restore_on_start(self.cfg, server)       # what the restart does
        self.assertEqual(server.usernames(), ["me"])
        self.assertEqual(S.key_file(), Path(self.data) / "secret.key")
        row = server.secret_rows()["flickr.api_key"]
        self.assertEqual(S.decrypt("flickr.api_key", row["nonce"], row["ciphertext"]), "laptop-flickr-key")
        self.assertFalse(any(n.startswith("restore.") for n in os.listdir(self.data)))
        self.assertEqual(TestClient(create_app(self.cfg, None, scheduler=False), base_url="https://testserver")
                         .get("/api/setup").json(), {"needed": False, "sqlite_found": False})


class FreshServerTest(unittest.TestCase):
    """A fresh deploy: no API keys and no Flickr login yet. It must start, wait, and pick
    the keys up from the Settings page without a restart (and never prompt)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"FLICKR_ORGANIZER_KEY_FILE": os.path.join(self.tmp, "secret.key")})
        self.env.start()
        self.addCleanup(self.env.stop)
        for var in ("FLICKR_API_KEY", "FLICKR_API_SECRET", "ANTHROPIC_API_KEY"):
            os.environ.pop(var, None)
        self.cfg_path = os.path.join(self.tmp, "config.yaml")
        with open(self.cfg_path, "w") as f:
            f.write("database: db.duckdb\nweb:\n  allowed_hosts: [testserver]\n")
        Store(os.path.join(self.tmp, "db.duckdb")).set_password_hash("owner", hash_password(PASSWORD))
        from flickr_organizer.cli import _new_client
        from flickr_organizer.config import load_config
        cfg = load_config(self.cfg_path)
        self.assertIsNone(_new_client(cfg))                                 # no keys: no client, no prompt
        self.built = []
        self.app = create_app(cfg, None, scheduler=False, cfg_path=self.cfg_path,
                              client_factory=lambda c: self.built.append(c["flickr"]["api_key"]) or FakeFlickr())
        self.c = TestClient(self.app, base_url="https://testserver")
        self.h = {"X-CSRF-Token": self.c.post("/api/auth/login", json={"username": "owner", "password": PASSWORD}).json()["csrf"]}

    def test_waits_for_keys_then_connects_without_a_restart(self):
        self.assertEqual(self.c.post("/api/sync/run", json={}, headers=self.h).json(), {"status": "no_flickr"})
        self.assertEqual(self.c.get("/api/admin/flickr").json()["connected"], False)
        self.assertEqual(self.c.post("/api/admin/flickr/connect", headers=self.h).status_code, 409)
        for name, value in (("flickr.api_key", "fresh-key-0001"), ("flickr.api_secret", "fresh-secret-0002")):
            r = self.c.put(f"/api/admin/secrets/{name}", json={"value": value, "password": PASSWORD}, headers=self.h)
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.built[-1], "fresh-key-0001")
        self.assertIsNotNone(self.app.state.runtime.client)
        self.assertEqual(self.c.post("/api/sync/run", json={"mode": "incremental"}, headers=self.h).json()["status"], "queued")


if __name__ == "__main__":
    unittest.main()
