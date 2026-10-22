"""Settings layers (defaults < config.yaml < admin page < environment), encrypted API
keys, and the admin API."""
import os
import stat
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from coconut_barometer import settings as S
from coconut_barometer.config import load_config, read_raw
from coconut_barometer.settings_meta import META
from coconut_barometer.store import Store
from coconut_barometer.sync import sync
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import hash_password

PASSWORD = "correct horse battery"
CONFIG = """database: db.duckdb
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
        self.key_file = os.path.join(self.tmp, "keys", "secret.key")
        for patch in (mock.patch.object(S, "DEFAULT_KEY_FILE", self.key_file),
                      mock.patch.object(S, "OLD_KEY_FILE", os.path.join(self.tmp, "old-keys", "secret.key")),
                      mock.patch.dict(os.environ)):
            patch.start()
            self.addCleanup(patch.stop)
        for var in ("DATABASE_PATH", "DATA_DIR"):
            os.environ.pop(var, None)
        self.store = Store(os.path.join(self.tmp, "db.duckdb"))

    def file_raw(self):
        return read_raw(self.cfg_path)


class LayersTest(Base):
    def test_admin_overrides_file_and_keys_never_come_from_the_environment(self):
        S.save(self.cfg_path, self.file_raw(), self.store, {"vision.model": "from-admin"}, [], "owner")
        self.assertEqual(load_config(self.cfg_path)["vision"]["model"], "from-admin")
        _, sources, _ = S.layer(self.file_raw(), self.store)
        self.assertEqual((sources["vision.model"], sources["flickr.calls_per_second"], sources["flickr.api_key"]),
                         ("admin", "default", "file"))
        with mock.patch.dict(os.environ, {"FLICKR_API_KEY": "env-key", "ANTHROPIC_API_KEY": "env-key"}):
            cfg = load_config(self.cfg_path)
            self.assertEqual((cfg["flickr"]["api_key"], S.layer(self.file_raw(), self.store)[1]["flickr.api_key"]),
                             ("file-key", "file"))

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

    def test_moved_settings_keep_their_value(self):
        """The gradient moved from map.* to theme.*: a colour saved under the old path still counts,
        in the database and in config.yaml, and the next save stores it under the new one."""
        self.store.save_admin_settings({"map.color_high": "#00ff00"}, [], "old", [])
        raw, sources, _ = S.layer(self.file_raw(), self.store)
        self.assertEqual((raw["theme"]["color_high"], sources["theme.color_high"]), ("#00ff00", "admin"))
        S.save(self.cfg_path, self.file_raw(), self.store, {"vision.concurrency": 9}, [], "owner")
        self.assertEqual(self.store.admin_settings(), {"theme.color_high": "#00ff00", "vision.concurrency": 9})
        from coconut_barometer.config import retire
        self.assertEqual(retire({"map": {"color_low": "#000000", "expand_at": 9}}),
                         {"map": {"expand_at": 9}, "theme": {"color_low": "#000000"}})

    def test_units_moved_out_of_weather(self):
        """Units now cover Weather and APRS: a choice saved under weather.units, on the page or in the file, carries over."""
        self.store.save_admin_settings({"weather.units": "imperial"}, [], "old", [])
        raw, sources, _ = S.layer(self.file_raw(), self.store)
        self.assertEqual((raw["display"]["units"], sources["display.units"]), ("imperial", "admin"))
        from coconut_barometer.config import retire
        self.assertEqual(retire({"weather": {"units": "imperial"}}), {"display": {"units": "imperial"}})

    def test_import_file_keeps_page_changes(self):
        import argparse, contextlib, io
        from coconut_barometer.cli import cmd_settings
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
        db = os.path.join(self.tmp, "db.duckdb")     # recent writes may still be in the write-ahead log
        dump = b"".join(open(f, "rb").read() for f in (db, db + ".wal") if os.path.exists(f))
        self.assertGreater(len(dump), 0)
        self.assertNotIn(b"sk-ant-very-secret", dump)
        self.assertEqual(load_config(self.cfg_path)["vision"]["api_key"], "sk-ant-very-secret-1234")
        self.assertIsNone(S.decrypt("flickr.api_key", row["nonce"], row["ciphertext"]))   # can't move to another field
        self.assertEqual(stat.S_IMODE(os.stat(self.key_file).st_mode), 0o600)
        self.assertEqual(row["hint"], "1234")

    def test_without_the_key_the_value_is_reported_not_guessed(self):
        S.set_secret(self.store, "flickr.api_key", "abcdefgh-ijkl-5678", "owner")
        os.remove(self.key_file)
        raw, _, problems = S.layer(self.file_raw(), self.store)
        self.assertEqual(raw["flickr"]["api_key"], "file-key")             # falls back to config.yaml
        self.assertTrue(any("flickr.api_key" in p for p in problems))

    def test_the_key_file_from_before_the_rename_is_still_used(self):
        old = S.OLD_KEY_FILE
        os.makedirs(os.path.dirname(old))
        with open(old, "w") as f:
            f.write(S.new_key())
        S.set_secret(self.store, "flickr.api_secret", "from-the-page-9999", "owner")
        self.assertEqual(load_config(self.cfg_path)["flickr"]["api_secret"], "from-the-page-9999")
        self.assertFalse(os.path.exists(self.key_file))                   # no second key made beside it
        os.makedirs(os.path.dirname(self.key_file))
        with open(self.key_file, "w") as f:                               # once the new one exists, it wins
            f.write(S.new_key())
        self.assertEqual(str(S.key_file()), self.key_file)

    def test_both_base64_alphabets_give_the_same_key(self):
        import base64
        raw = bytes(range(200, 232))          # encodes with "+" and "/" in standard base64, "-" and "_" url-safe
        std, url = base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode()
        self.assertTrue(set("+/") & set(std) and set("-_") & set(url))
        for text in (std, url, std.rstrip("="), url.rstrip("=")):
            self.assertEqual(S._key_from_text(text), raw, text)


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

    def test_the_encryption_key_downloads_and_opens_a_backup_elsewhere(self):
        from coconut_barometer import backup as B
        get = lambda pw: self.c.post("/api/admin/encryption-key/download", json={"password": pw}, headers=self.h)
        self.assertEqual(get(PASSWORD).status_code, 404)                 # no API key saved yet, so no key
        self.c.put("/api/admin/secrets/flickr.api_key", json={"value": "new-flickr-key-0001", "password": PASSWORD},
                   headers=self.h)
        self.assertEqual(get("nope").status_code, 403)
        self.assertEqual(self.c.post("/api/admin/encryption-key/download", json={"password": PASSWORD}).status_code, 403)
        r = get(PASSWORD)
        self.assertEqual((r.status_code, r.headers["cache-control"]), (200, "no-store"))
        self.assertIn('filename="secret.key"', r.headers["content-disposition"])
        backup = B.make_backup(self.app.state.runtime.cfg)
        self.assertEqual(B.check_backup_file(str(backup), S._key_from_text(r.text)), {"keys": 1, "readable": True})
        self.assertFalse(B.check_backup_file(str(backup), S._key_from_text(S.new_key()))["readable"])

    def test_password_change_signs_out_and_sessions_can_be_ended(self):
        listed = self.c.get("/api/admin/sessions").json()
        self.assertEqual([s["current"] for s in listed], [True])
        r = self.c.post("/api/admin/password", json={"current": PASSWORD, "new": "an even longer passphrase"}, headers=self.h)
        self.assertEqual(r.json()["signed_out"], True)
        self.assertEqual(self.c.get("/api/admin/settings").status_code, 401)
        self.assertEqual(self.c.post("/api/auth/login", json={"username": "owner", "password": "an even longer passphrase"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
