"""Settings layers (defaults < config.yaml < admin page < environment), encrypted API
keys, and the admin API."""
import os
import stat
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from flickr_organizer import settings as S
from flickr_organizer.config import load_config, read_raw
from flickr_organizer.settings_meta import META
from flickr_organizer.store import Store
from flickr_organizer.sync import sync
from flickr_organizer.web.app import create_app
from flickr_organizer.web.security import hash_password

PASSWORD = "correct horse battery"
CONFIG = """database: db.sqlite
flickr:
  api_key: "file-key"
  api_secret: "file-secret"
vision:
  model: from-file
  api_key: "file-vision-key"
  categories:
    food: {album: Food, description: food, tags: [food]}
web:
  allowed_hosts: [testserver]
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg_path = os.path.join(self.tmp, "config.yaml")
        with open(self.cfg_path, "w") as f:
            f.write(CONFIG)
        self.env = mock.patch.dict(os.environ, {"FLICKR_ORGANIZER_KEY_FILE": os.path.join(self.tmp, "keys", "secret.key")})
        self.env.start()
        for var in ("FLICKR_API_KEY", "FLICKR_API_SECRET", "ANTHROPIC_API_KEY", "VISION_API_KEY", S.KEY_ENV,
                    "FLICKR_ORGANIZER_DATABASE"):
            os.environ.pop(var, None)
        self.addCleanup(self.env.stop)
        self.store = Store(os.path.join(self.tmp, "db.sqlite"))

    def file_raw(self):
        return read_raw(self.cfg_path)


class LayersTest(Base):
    def test_admin_overrides_file_and_env_overrides_admin(self):
        S.save(self.cfg_path, self.file_raw(), self.store, {"vision.model": "from-admin"}, [], "owner")
        self.assertEqual(load_config(self.cfg_path)["vision"]["model"], "from-admin")
        _, sources, _ = S.layer(self.file_raw(), self.store)
        self.assertEqual((sources["vision.model"], sources["flickr.calls_per_second"], sources["flickr.api_key"]),
                         ("admin", "default", "file"))
        with mock.patch.dict(os.environ, {"FLICKR_API_KEY": "env-key"}):
            self.assertEqual(load_config(self.cfg_path)["flickr"]["api_key"], "env-key")
            self.assertEqual(S.layer(self.file_raw(), self.store)[1]["flickr.api_key"], "env")

    def test_save_validates_first_and_refuses_locked_or_unknown_settings(self):
        with self.assertRaises(S.SettingsError) as e:
            S.save(self.cfg_path, self.file_raw(), self.store, {"vision.min_confidence": 7}, [], "owner")
        self.assertIn("vision.min_confidence: 7 is outside 0..1", e.exception.errors)
        self.assertEqual(self.store.admin_settings(), {})                  # nothing stored
        for path in ("web.allowed_hosts", "web.secure_cookies", "database", "vision.api_key", "vision.nope"):
            with self.assertRaises(S.SettingsError):
                S.save(self.cfg_path, self.file_raw(), self.store, {path: "x"}, [], "owner")

    def test_file_values_stay_in_the_database_and_defaults_drop_out(self):
        """A server has no config.yaml: the database must carry the setting even when it
        matches the file. Only a value equal to the built-in default is dropped."""
        S.save(self.cfg_path, self.file_raw(), self.store, {"vision.model": "other"}, [], "owner")
        S.save(self.cfg_path, self.file_raw(), self.store, {"vision.model": "from-file"}, [], "owner")
        self.assertEqual(self.store.admin_settings(), {"vision.model": "from-file"})
        S.save(self.cfg_path, self.file_raw(), self.store, {"vision.concurrency": 9}, [], "owner")
        self.assertEqual(self.store.admin_settings(), {"vision.model": "from-file", "vision.concurrency": 9})
        S.save(self.cfg_path, self.file_raw(), self.store, {"vision.concurrency": 4}, [], "owner")   # the default
        self.assertEqual(self.store.admin_settings(), {"vision.model": "from-file"})
        self.assertEqual([r["path"] for r in self.store.settings_log()][:2], ["vision.concurrency", "vision.concurrency"])

    def test_import_file_keeps_page_changes(self):
        import argparse, contextlib, io
        from flickr_organizer.cli import cmd_settings
        S.save(self.cfg_path, self.file_raw(), self.store, {"vision.model": "from-page"}, [], "owner")
        args = argparse.Namespace(action="import-file", file=None, config=self.cfg_path, overwrite=False, blank=False)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cmd_settings({"database": self.store.path}, self.store, args)
        self.assertEqual(self.store.admin_settings()["vision.model"], "from-page")
        self.assertIn("Kept what the admin page has for: vision.model", out.getvalue())
        args.overwrite = True
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_settings({"database": self.store.path}, self.store, args)
        self.assertEqual(self.store.admin_settings()["vision.model"], "from-file")

    def test_every_setting_has_a_label(self):
        self.assertEqual([p for p in S.leaves() if p not in META], [])


class SecretsTest(Base):
    def test_keys_are_encrypted_at_rest_and_bound_to_their_name(self):
        S.set_secret(self.store, "vision.api_key", "sk-ant-very-secret-1234", "owner")
        row = self.store.secret_rows()["vision.api_key"]
        self.assertNotIn(b"very-secret", row["ciphertext"])
        db = os.path.join(self.tmp, "db.sqlite")     # recent writes may still be in the WAL file
        dump = b"".join(open(f, "rb").read() for f in (db, db + "-wal") if os.path.exists(f))
        self.assertGreater(len(dump), 0)
        self.assertNotIn(b"sk-ant-very-secret", dump)
        self.assertIn(b"vision.api_key", dump)        # (the row is there, just not readable)
        self.assertEqual(load_config(self.cfg_path)["vision"]["api_key"], "sk-ant-very-secret-1234")
        self.assertIsNone(S.decrypt("flickr.api_key", row["nonce"], row["ciphertext"]))   # can't move to another field
        key_file = os.environ["FLICKR_ORGANIZER_KEY_FILE"]
        self.assertEqual(stat.S_IMODE(os.stat(key_file).st_mode), 0o600)
        self.assertEqual(row["hint"], "1234")

    def test_without_the_key_the_value_is_reported_not_guessed(self):
        S.set_secret(self.store, "flickr.api_key", "abcdefgh-ijkl-5678", "owner")
        os.remove(os.environ["FLICKR_ORGANIZER_KEY_FILE"])
        raw, _, problems = S.layer(self.file_raw(), self.store)
        self.assertEqual(raw["flickr"]["api_key"], "file-key")             # falls back to config.yaml
        self.assertTrue(any("flickr.api_key" in p for p in problems))

    def test_key_from_the_environment(self):
        with mock.patch.dict(os.environ, {S.KEY_ENV: S.new_key()}):
            S.set_secret(self.store, "flickr.api_secret", "from-the-page-9999", "owner")
            self.assertEqual(load_config(self.cfg_path)["flickr"]["api_secret"], "from-the-page-9999")
        self.assertFalse(os.path.exists(os.environ["FLICKR_ORGANIZER_KEY_FILE"]))   # no key file made


class AdminApiTest(Base):
    def setUp(self):
        super().setUp()
        sync(FakeFlickr(), self.store, log=lambda *a: None)
        self.store.set_password_hash("owner", hash_password(PASSWORD))
        cfg = load_config(self.cfg_path)
        self.clients = []
        self.app = create_app(cfg, FakeFlickr(), provider_factory=lambda: FakeVision("m1"), scheduler=False,
                              cfg_path=self.cfg_path, client_factory=lambda c: self.clients.append(c["flickr"]["api_key"]) or FakeFlickr())
        self.c = TestClient(self.app, base_url="https://testserver")
        self.h = {"X-CSRF-Token": self.c.post("/api/auth/login", json={"username": "owner", "password": PASSWORD}).json()["csrf"]}

    def setting(self, payload, path):
        return next(s for s in payload["settings"] if s["path"] == path)

    def test_settings_page_never_shows_keys(self):
        r = self.c.get("/api/admin/settings")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("file-vision-key", r.text)
        key = self.setting(r.json(), "vision.api_key")
        self.assertEqual((key["value"], key["set"], key["kind"]), (None, True, "secret"))
        self.assertTrue(self.setting(r.json(), "web.allowed_hosts")["locked"])

    def test_retired_settings_in_the_database_dont_block_saving(self):
        self.store.save_admin_settings({"rename.enabled": True, "vision.describe.enabled": False}, [], "old", [])
        r = self.c.put("/api/admin/settings", json={"changes": {"schedule.new_photos": "process"}}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(set(self.store.admin_settings()), {"schedule.new_photos"})     # stale rows gone
        choice = self.setting(r.json(), "schedule.new_photos")
        self.assertEqual((choice["kind"], choice["group"], choice["section"]), ("choice", "After a sync", "sync"))
        self.assertIn("right away", choice["option_labels"]["process"])
        self.assertEqual(self.app.state.runtime.cfg["schedule"]["new_photos"], "process")

    def test_no_switches_for_the_proposals(self):
        paths = {s["path"] for s in self.c.get("/api/admin/settings").json()["settings"]}
        for gone in ("vision.enabled", "vision.describe.enabled", "vision.title.enabled", "vision.title.replace",
                     "vision.extra_tags.enabled", "geo.enabled", "rename.enabled", "web.process_new",
                     "standard_tags.place.split_words", "standard_tags.coords.enabled", "standard_tags.owner.tags",
                     "standard_tags.filler.enabled"):
            self.assertNotIn(gone, paths)

    def test_saving_applies_without_a_restart(self):
        r = self.c.put("/api/admin/settings", json={"changes": {"web.sync_every_minutes": 15, "vision.model": "m2"}},
                       headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sorted(r.json()["saved"]), ["vision.model", "web.sync_every_minutes"])
        rt = self.app.state.runtime
        self.assertEqual((rt.cfg["web"]["sync_every_minutes"], rt.review.cfg["vision"]["model"]), (15, "m2"))
        bad = self.c.put("/api/admin/settings", json={"changes": {"vision.min_confidence": 2}}, headers=self.h)
        self.assertEqual(bad.status_code, 422)
        self.assertIn("vision.min_confidence: 2 is outside 0..1", bad.json()["detail"])
        self.assertEqual(self.c.put("/api/admin/settings", json={"changes": {}}).status_code, 403)   # no CSRF token

    def test_changing_a_key_needs_the_password_and_rebuilds_the_client(self):
        wrong = self.c.put("/api/admin/secrets/flickr.api_key", json={"value": "new-flickr-key-0001", "password": "nope"},
                           headers=self.h)
        self.assertEqual(wrong.status_code, 403)
        r = self.c.put("/api/admin/secrets/flickr.api_key", json={"value": "new-flickr-key-0001", "password": PASSWORD},
                       headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("new-flickr-key", r.text)
        self.assertEqual(self.setting(r.json(), "flickr.api_key")["hint"], "0001")
        self.assertEqual(self.clients, ["new-flickr-key-0001"])
        self.assertEqual(self.app.state.runtime.cfg["flickr"]["api_key"], "new-flickr-key-0001")

    def test_password_change_signs_out_and_sessions_can_be_ended(self):
        listed = self.c.get("/api/admin/sessions").json()
        self.assertEqual([s["current"] for s in listed], [True])
        r = self.c.post("/api/admin/password", json={"current": PASSWORD, "new": "an even longer passphrase"}, headers=self.h)
        self.assertEqual(r.json()["signed_out"], True)
        self.assertEqual(self.c.get("/api/admin/settings").status_code, 401)
        self.assertEqual(self.c.post("/api/auth/login", json={"username": "owner", "password": "an even longer passphrase"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
