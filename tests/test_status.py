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
        self.assertEqual(set(self.rows(d)), {"weatherflow", "aprs", "imagery", "lightning", "fronts", "hazards"})
        self.assertTrue(all(r["detail"] is None and r["href"] is None for r in d["rows"]))
        self.login()
        self.assertEqual(set(self.rows(self.get())), {"weatherflow", "aprs", "igate", "imagery", "radar", "lightning", "fronts",
                                                      "hazards", "qc", "cwop", "flickr", "vision", "backups", "visitors", "errors"})

    def test_the_igate_row(self):
        """Off (can't tell) until APRS-IS has been connected a while; then the last hour against its usual, red when
        it's gone quiet. Visitors see it too, once it says something."""
        now, gate = int(time.time()), "WA5DTR-15"
        self.assertEqual(self.rows(self.get())["igate"]["level"] if "igate" in self.rows(self.get()) else "off", "off")
        hub = self.app.state.runtime.aprs.hub
        hub.update(state="connected", since=now - 6 * 3600)
        save = lambda ts: self.store.igate_save(gate, {"from": "N5XYZ-9"}, True, ts)
        for week in range(1, 4):                                   # 20 an hour, the last three weeks
            for h in range(-26, 2):
                for i in range(20):
                    save(now - week * 7 * 86400 + h * 3600 + i * 60)
        for i in range(3 * 20):                                    # as usual for the last three hours
            save(now // 3600 * 3600 - 3 * 3600 + i * 180)
        self.store.db.execute("DELETE FROM aprs_igate WHERE ts > ?", (now - 3600,))
        for i in range(15):
            save(now - 3000 + i * 60)
        r = self.rows(self.get())["igate"]
        self.assertEqual(r["level"], "ok")
        self.assertIn("15 gated in the last hour (usually ~20)", r["state"])
        self.store.db.execute("DELETE FROM aprs_igate WHERE ts > ?", (now - 2 * 3600,))
        r = self.rows(self.get())["igate"]
        self.assertEqual(r["level"], "bad")
        self.assertIn("not gating", r["state"])

    def test_the_weather_maps_feeds_each_have_a_dot(self):
        """Lightning, fronts, and watches and warnings: read from what each last fetched (never fetched for the panel);
        a failed check warns, data already old when checked warns and then turns red, nothing at all is red."""
        from coconut_barometer.weather.fronts import fronts
        from coconut_barometer.weather.glm import glm
        from coconut_barometer.weather.hazards import hazards
        import numpy as np

        for f in (glm, fronts, hazards):                                 # the process's own feeds: put back after
            for k in ("data", "checked_at", "error"):
                self.addCleanup(setattr, f, k, getattr(f, k))
                setattr(f, k, None)
        self.login()                                                     # signed in: with the error text
        now = time.time()
        r = self.rows(self.get())
        self.assertEqual([r[k]["level"] for k in ("lightning", "fronts", "hazards")], ["ok"] * 3)   # not asked for yet
        self.assertIn("checked when the weather map shows it", r["lightning"]["state"])

        glm.data = {"t": np.zeros(3, dtype=np.int64), "lat": np.zeros(3), "lon": np.zeros(3), "newest": int(now) - 60}
        glm.checked_at = now
        fronts.data, fronts.checked_at = {"valid": int(now) - 7 * 3600, "fronts": [{}, {}], "centers": []}, now
        hazards.data, hazards.checked_at, hazards.error = None, now, "ConnectionError: down"
        r = self.rows(self.get())
        self.assertEqual((r["lightning"]["level"], r["fronts"]["level"], r["hazards"]["level"]), ("ok", "warn", "bad"))
        self.assertIn("3 flashes held", r["lightning"]["state"])
        self.assertIn("2 fronts", r["fronts"]["state"])
        self.assertEqual(r["hazards"]["detail"], "ConnectionError: down")

        glm.data["newest"], glm.error = int(now) - 40 * 60, "RuntimeError: THREDDS"
        hazards.data, hazards.error = {"alerts": [{"minor": False}, {"minor": True}]}, None
        r = self.rows(self.get())
        self.assertEqual((r["lightning"]["level"], r["hazards"]["level"]), ("bad", "ok"))
        self.assertIn("1 watch and warnings, 1 advisories", r["hazards"]["state"])

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
