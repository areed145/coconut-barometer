"""The health of each connection (/api/status), for Home, the Status page and the top bar's dot: a row per
connection, from what the app already tracks."""
import os
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from coconut_barometer import activity
from coconut_barometer.config import from_dict
from coconut_barometer.store import Store
from coconut_barometer.web.api import status as status_api
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import hash_password

STATION = {"station_id": 7, "name": "Backyard", "latitude": 30.27, "longitude": -97.74, "timezone": "America/Chicago",
           "station_meta": {"elevation": 150.0}, "devices": [{"device_id": 11, "device_type": "ST", "serial_number": "ST-1"}]}


def arr(ts):
    return [ts, 1.0, 3.0, 5.0, 90.0, 3, 1000.0, 20.0, 50.0, 10000, 2.0, 300.0, 0.0, 0, 0, 0, 2.7, 1]


class StatusTest(unittest.TestCase):
    def setUp(self):
        status_api._cache.clear()
        self.addCleanup(status_api._cache.clear)
        self.addCleanup(status_api._flickr_check.update, at=0.0, status=None)
        self.path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        self.store = Store(self.path)
        self.store.set_password_hash("owner", hash_password("correct horse battery"))
        self.store.wx_save_stations([STATION])
        self.cfg = from_dict({**CFG, "database": self.path, "web": {"allowed_hosts": ["testserver"]},
                              "weatherflow": {"token": "tok"}, "aprs": {"callsigns": ["KK6GPV-9"]}})
        self.app = create_app(self.cfg, FakeFlickr(), provider_factory=lambda: FakeVision("m"), scheduler=False)
        self.c = TestClient(self.app, base_url="https://testserver")

    def get(self):
        status_api._cache.clear()
        return self.c.get("/api/status").json()

    def rows(self, d):
        return {r["id"]: r for r in d["rows"]}

    def login(self):
        self.c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"})

    def test_visitors_get_the_public_services_without_errors(self):
        d = self.get()
        self.assertEqual(set(self.rows(d)), {"weatherflow", "aprs", "imagery"})
        self.assertTrue(all(r["detail"] is None and r["href"] is None for r in d["rows"]))
        self.login()
        self.assertEqual(set(self.rows(self.get())), {"weatherflow", "aprs", "imagery", "radar", "cwop", "flickr", "vision", "backups", "errors"})

    def test_a_station_gone_quiet_turns_weatherflow_red(self):
        now = int(time.time())
        self.store.wx_save_obs(11, [arr(now - 120)])
        wf = self.rows(self.get())["weatherflow"]
        self.assertEqual(wf["level"], "warn")                         # reporting, but the stream isn't connected here
        self.assertIn("1 station reporting", wf["state"])
        self.store.wx_save_obs(11, [arr(now - 2 * 3600)])
        self.store.db.execute("DELETE FROM wx_obs WHERE ts > ?", (now - 3600,))
        self.store.db.commit()
        wf = self.rows(self.get())["weatherflow"]
        self.assertEqual(wf["level"], "bad")
        self.assertIn("Backyard: last report 2 h ago", wf["state"])
        self.assertEqual(self.get()["overall"], "bad")

    def test_backups_and_the_overall_state(self):
        self.login()
        now = int(time.time())
        self.store.set_meta("last_backup", now - 3600)
        self.assertEqual(self.rows(self.get())["backups"]["level"], "ok")
        self.store.set_meta("last_backup_error", f"{now}: disk full")
        b = self.rows(self.get())["backups"]
        self.assertEqual((b["level"], b["detail"], b["href"]), ("bad", "disk full", "#/settings/backup"))
        self.cfg["backup"]["every_hours"] = 0
        self.assertEqual(self.rows(self.get())["backups"]["level"], "off")
        self.assertIn(self.get()["overall"], ("warn", "bad"))                # Flickr isn't signed in, among others

    def test_the_activity_log_reports_the_newest_error_and_last_success(self):
        log = activity.Activity()
        with log.call("vision", "classify"):
            pass
        with self.assertRaises(RuntimeError):
            with log.call("vision", "classify"):
                raise RuntimeError("quota exceeded")
        last = log.latest("vision")
        self.assertIn("quota exceeded", last["error"])
        self.assertLessEqual(last["ok_at"], last["at"])
        self.assertEqual(log.latest("nothing"), {"at": None, "ok_at": None, "error": None})


if __name__ == "__main__":
    unittest.main()
