"""Alerts (alerts.py) and Web Push (push.py): what's worth a notification, the cool-down, the devices turned on,
and a push service saying a device is gone."""
import base64
import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from coconut_barometer import push
from coconut_barometer import settings as S
from coconut_barometer.alerts import Alerts
from coconut_barometer.config import from_dict
from coconut_barometer.store import Store
from coconut_barometer.web.app import STATIC, create_app
from coconut_barometer.web.security import hash_password

STATION = {"station_id": 7, "name": "Backyard", "latitude": 30.27, "longitude": -97.74, "timezone": "America/Chicago",
           "station_meta": {"elevation": 0}, "devices": [{"device_id": 11, "device_type": "ST", "serial_number": "ST-1"}]}
SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "BPk", "auth": "au"}}


def obs(ts, gust=5.0, pressure=1013.0, strike_dist=0, strike_count=0):
    return [ts, 1.0, 3.0, gust, 90.0, 3, pressure, 20.0, 50.0, 10000, 2.0, 300.0, 0.0, 0, strike_dist, strike_count, 2.7, 1]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        patch = mock.patch.object(S, "DEFAULT_KEY_FILE", os.path.join(self.tmp, "keys", "secret.key"))
        patch.start()
        self.addCleanup(patch.stop)
        self.path = os.path.join(self.tmp, "db.duckdb")
        self.store = Store(self.path)
        self.store.wx_save_stations([STATION])
        self.cfg = from_dict({**CFG, "database": self.path, "aprs": {"callsigns": ["KK6GPV-9"]}})


class AlertsTest(Base):
    def setUp(self):
        super().setUp()
        self.now = int(time.time())
        self.alerts = Alerts(self.cfg, lambda: ["KK6GPV-9"])
        self.alerts.since = self.now - 60

    def kinds(self):
        return {a["key"].split(":")[0]: a for a in self.alerts.check(self.store, self.now)}

    def test_lightning_nearby_but_not_far_away(self):
        self.store.wx_save_event(11, self.now - 30, "strike", 30.0, 1)
        self.assertEqual(self.kinds(), {})
        self.alerts.since = self.now - 60
        self.store.wx_save_event(11, self.now - 20, "strike", 8.0, 1)
        alert = self.kinds()["lightning"]
        self.assertEqual(alert["title"], "Lightning 8 km from Backyard")
        self.assertEqual(alert["evidence"]["matching_records"], 1)

    def test_a_strike_in_an_observation_counts_too_and_imperial_is_in_miles(self):
        self.cfg["display"]["units"] = "imperial"
        self.store.wx_save_obs(11, [obs(self.now - 30, strike_dist=10, strike_count=2)])
        self.assertEqual(self.kinds()["lightning"]["title"], "Lightning 6 mi from Backyard")

    def test_a_strong_gust(self):
        self.store.wx_save_obs(11, [obs(self.now - 30, gust=20.0)])     # 72 km/h
        self.assertEqual(self.kinds()["gust"]["title"], "Gust of 72 km/h at Backyard")

    def test_falling_pressure(self):
        self.store.wx_save_obs(11, [obs(self.now - 3 * 3600 - 60, pressure=1010.0), obs(self.now - 30, pressure=1007.5)])
        self.assertIn("Down 2.5 hPa in 3 hours", self.kinds()["pressure"]["body"])

    def test_a_callsign_heard_after_quiet(self):
        packet = {"from": "KK6GPV-9", "latitude": 30.0, "longitude": -97.0}
        self.store.aprs_save(packet, self.now - 9 * 3600)
        self.store.aprs_save(packet, self.now - 30)
        self.store.aprs_save(packet, self.now - 10)
        self.assertEqual(self.kinds()["callsign"]["body"], "Its first packet in 9 hours.")
        self.alerts.since = self.now
        self.store.aprs_save(packet, self.now + 30)
        self.assertEqual(self.alerts.check(self.store, self.now + 60), [])

    def test_the_igate_hears_a_balloon(self):
        self.alerts.igate = lambda: "WA5DTR-15"
        self.cfg["alerts"]["igate_altitude_km"] = 5
        save = lambda alt, ts, call="KD5BAL-11": self.store.igate_save(
            "WA5DTR-15", {"from": call, "latitude": 30.5, "longitude": -97.2, "altitude": alt, "symbol_table": "/", "symbol": "O"}, True, ts)
        save(4000, self.now - 40, "N5LOW-9")                 # an airliner's height it isn't: under the line
        save(9000, self.now - 30)
        save(12000, self.now - 20)
        a = self.kinds()["altitude"]
        self.assertEqual((a["key"], a["title"], a["evidence"]["altitude_m"]), ("altitude:KD5BAL-11", "KD5BAL-11 heard at 12.0 km", 12000))
        self.alerts.since = self.now
        self.cfg["alerts"]["igate_altitude_km"] = 0
        save(13000, self.now + 30)
        self.assertEqual(self.alerts.check(self.store, self.now + 60), [])

    def test_the_igate_goes_quiet_and_comes_back(self):
        gate = "WA5DTR-15"
        stream = {"state": "connected", "since": self.now - 6 * 3600}
        self.alerts.igate, self.alerts.stream = (lambda: gate), (lambda: stream)
        save = lambda ts: self.store.igate_save(gate, {"from": "N5XYZ-9"}, True, ts)
        for week in range(1, 4):                                   # 20 packets an hour, the last three weeks
            for h in range(-26, 2):
                for i in range(20):
                    save(self.now - week * 7 * 86400 + h * 3600 + i * 180)
        save(self.now - 2 * 3600)                                  # then nothing for two hours
        igate = lambda: {k: v for k, v in self.kinds().items() if k == "igate"}
        a = igate()["igate"]
        self.assertEqual((a["key"], a["title"]), ("igate:gating", "WA5DTR-15 has gone quiet"))
        self.assertIn("its last packet 2 hours ago; it usually gates about 20 an hour", a["body"])
        self.alerts.since = self.now - 60
        self.assertEqual(igate(), {})                              # told once
        stream["state"] = "reconnecting"                           # APRS-IS down: nothing said either way
        save(self.now - 30)
        self.alerts.since = self.now - 60
        self.assertEqual(igate(), {})
        stream["state"] = "connected"
        self.alerts.since = self.now - 60
        self.assertEqual(igate(), {})                              # one packet in three hours: still well under its usual
        for i in range(60):                                        # its usual 20 an hour again
            save(self.now // 3600 * 3600 - 3 * 3600 + i * 180)
        self.alerts.since = self.now - 60
        self.assertEqual(igate()["igate"]["key"], "igate:gating-back")

    def test_the_igate_is_not_blamed_without_a_baseline_or_a_connection(self):
        gate = "WA5DTR-15"
        self.alerts.igate = lambda: gate
        self.alerts.stream = lambda: {"state": "connected", "since": self.now - 6 * 3600}
        self.store.igate_save(gate, {"from": "N5XYZ-9"}, True, self.now - 3 * 3600)    # no history: no expectation
        self.assertNotIn("igate", self.kinds())
        self.alerts.stream = lambda: {"state": "connected", "since": self.now - 600}  # only just reconnected
        self.alerts.since = self.now - 60
        self.assertNotIn("igate", self.kinds())

    def test_the_igate_beacon_stops(self):
        gate = "WA5DTR-15"
        self.alerts.igate = lambda: gate
        self.alerts.stream = lambda: {"state": "connected", "since": self.now - 6 * 3600}
        self.store.igate_place(gate, 30.0, -97.0, self.now - 4 * 3600)
        self.assertEqual(self.kinds()["igate"]["title"], "WA5DTR-15's beacon has stopped")
        self.store.igate_place(gate, 30.0, -97.0, self.now - 60)
        self.alerts.since = self.now - 60
        self.assertEqual(self.kinds()["igate"]["key"], "igate:beacon-back")

    def test_zero_turns_one_off(self):
        self.cfg["alerts"]["gust_kmh"] = 0
        self.store.wx_save_obs(11, [obs(self.now - 30, gust=40.0)])
        self.assertEqual(self.kinds(), {})

    def test_cooldown_and_nothing_checked_without_a_device(self):
        at = self.now - 3 * 86400
        self.store.wx_save_obs(11, [obs(at - 30, gust=20.0)])
        self.assertEqual(self.alerts.run_once(self.store, at), [])     # no device
        push.subscribe(self.store, SUB, "owner", "Phone", "https://site.example")
        with mock.patch.object(push, "send") as send:
            send.return_value = {"sent": 1, "failed": [], "removed": 0}
            self.alerts.since = at - 60
            self.assertEqual(len(self.alerts.run_once(self.store, at)), 1)
            audit = self.store.alert_log_recent()
            self.assertEqual(len(audit), 1)
            self.assertEqual((audit[0]["status"], audit[0]["device_label"]), ("sent", "Phone"))
            self.assertEqual(audit[0]["evidence"]["gust_ms"], 20.0)
            day = datetime.fromtimestamp(at, timezone.utc).date()
            self.assertEqual(audit[0]["url"], f"/#/alerts/{at}/gust%3A11")             # its own page (#73) ...
            self.assertEqual(audit[0]["evidence"]["link"],                               # ... which links the chart
                             f"/#/weather/history?from={(day - timedelta(days=1)).isoformat()}"
                             f"&to={(day + timedelta(days=1)).isoformat()}&station=11&chart=wind")
            self.assertEqual(send.call_args.args[3], f"/#/alerts/{at}/gust%3A11")       # where the notification opens
            self.store.wx_save_obs(11, [obs(at + 30, gust=21.0)])
            self.assertEqual(self.alerts.run_once(self.store, at + 60), [])            # cooling down
            self.store.wx_save_obs(11, [obs(at + 3630, gust=21.0)])
            self.assertEqual(len(self.alerts.run_once(self.store, at + 3660)), 1)     # an hour on
        self.assertEqual(send.call_count, 2)
        self.assertEqual(len(self.store.alert_log_recent()), 2)
        self.assertEqual(send.call_args.kwargs["tag"], "gust:11")

    def test_failed_delivery_is_kept_in_the_audit_log(self):
        self.store.wx_save_obs(11, [obs(self.now - 30, gust=20.0)])
        push.subscribe(self.store, SUB, "owner", "Phone", "https://site.example")
        with mock.patch.object(push, "send", return_value={"sent": 0, "failed": ["Phone: 503 unavailable"],
                                                           "removed": 0}):
            self.assertEqual(len(self.alerts.run_once(self.store, self.now)), 1)
        audit = self.store.alert_log_recent()
        self.assertEqual(audit[0]["status"], "failed")
        self.assertEqual(audit[0]["detail"], "Phone: 503 unavailable")

    def test_push_exception_is_kept_in_the_audit_log(self):
        self.store.wx_save_obs(11, [obs(self.now - 30, gust=20.0)])
        push.subscribe(self.store, SUB, "owner", "Phone", "https://site.example")
        with mock.patch.object(push, "send", side_effect=RuntimeError("VAPID key unavailable")):
            self.assertEqual(len(self.alerts.run_once(self.store, self.now)), 1)
        audit = self.store.alert_log_recent()
        self.assertEqual(audit[0]["status"], "failed")
        self.assertEqual(audit[0]["detail"], "RuntimeError: VAPID key unavailable")

    def test_storm_audit_preserves_the_radar_scan_time(self):
        cell = {"id": "K-1", "valid": self.now - 120, "lat": 30.27, "lon": -97.74, "dbz": 55,
                "posh": 20, "tvs": True, "kmh": 36, "toward": 90}

        class Radar:
            def snapshot(self):
                return {"cells": [cell]}

            def stations(self, _store):
                return [{"name": "Backyard", "device_id": 11, "lat": 30.27, "lon": -97.74}]

            def closing(self, _device, _now):
                return None

        with mock.patch("coconut_barometer.alerts.rd.arrival", return_value=60):
            alert = next(x for x in Alerts(self.cfg, radar=Radar()).check(self.store, self.now)
                         if x["key"].startswith("storm:11:"))
        self.assertEqual(alert["evidence"]["radar_observed_at"], cell["valid"])


class PushTest(Base):
    def test_the_key_is_made_once_and_kept_encrypted(self):
        key = push.public_key(self.store)
        self.assertEqual(push.public_key(self.store), key)
        self.assertEqual(len(key), 87)                                      # 65 bytes, base64url
        self.assertNotIn("PRIVATE KEY", self.store.get_meta(push.KEY_META))

    def test_a_device_gone_from_its_push_service_is_removed(self):
        push.subscribe(self.store, SUB, "owner", "Phone", "https://site.example")
        push.subscribe(self.store, {**SUB, "endpoint": "https://push.example/def"}, "owner", "Laptop", "https://site.example")
        from pywebpush import WebPushException

        def webpush(sub, data, **kw):
            self.assertEqual(kw["vapid_claims"], {"sub": "https://site.example"})
            if sub["endpoint"].endswith("abc"):
                raise WebPushException("gone", response=mock.Mock(status_code=410))
            self.assertEqual(json.loads(data)["title"], "Hi")
        with mock.patch("pywebpush.webpush", webpush):
            self.assertEqual(push.send(self.store, "Hi", "there"), {"sent": 1, "failed": [], "removed": 1})
        self.assertEqual([d["label"] for d in push.devices(self.store)], ["Laptop"])

    def test_not_a_subscription(self):
        with self.assertRaises(ValueError):
            push.subscribe(self.store, {"endpoint": "http://x", "keys": {}}, "owner", "x", "")


class PushApiTest(Base):
    def setUp(self):
        super().setUp()
        self.store.set_password_hash("owner", hash_password("correct horse battery"))
        self.cfg["web"]["allowed_hosts"] = ["testserver"]
        self.c = TestClient(create_app(self.cfg, FakeFlickr(), provider_factory=lambda: FakeVision("m"), scheduler=False),
                            base_url="https://testserver")

    def test_signed_in_only(self):
        self.assertEqual(self.c.get("/api/push").status_code, 401)
        self.assertEqual(self.c.get("/api/push/history").status_code, 401)

    def test_history_returns_alert_evidence_for_signed_in_users(self):
        self.store.alert_log_add(
            123, {"key": "gust:11", "title": "Gust", "body": "Wind", "url": "/#/weather/map",
                  "evidence": {"gust_ms": 20.0}},
            {"id": "push-id", "label": "Phone", "username": "owner"}, "sent")
        self.assertEqual(self.c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"}).status_code, 200)
        response = self.c.get("/api/push/history")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["alerts"][0]["evidence"], {"gust_ms": 20.0})
        self.assertEqual(response.json()["alerts"][0]["status"], "sent")

    def test_an_alert_has_its_own_page(self):
        """#73: the notification opens #/alerts/<at>/<key>; /api/push/alert gives it, with every device's delivery."""
        alert = {"key": "storm:11:HGX-A1", "title": "Storm ~20 min from Backyard", "body": "55 dBZ, 30 km W, moving E.",
                 "url": "/#/alerts/1800000000/storm%3A11%3AHGX-A1", "evidence": {"dbz": 55, "link": "/#/weather/map"}}
        for label, status in (("Phone", "sent"), ("Laptop", "failed")):
            self.store.alert_log_add(1_800_000_000, alert, {"id": label, "label": label, "username": "owner"}, status)
        self.assertEqual(self.c.get("/api/push/alert?at=1800000000&key=storm:11:HGX-A1").status_code, 401)
        self.c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"})
        d = self.c.get("/api/push/alert", params={"at": 1_800_000_000, "key": "storm:11:HGX-A1"}).json()
        self.assertEqual((d["title"], d["evidence"]["dbz"], d["evidence"]["link"]), (alert["title"], 55, "/#/weather/map"))
        self.assertEqual([(x["device_label"], x["status"]) for x in d["deliveries"]], [("Phone", "sent"), ("Laptop", "failed")])
        self.assertEqual(self.c.get("/api/push/alert", params={"at": 1, "key": "storm:11:HGX-A1"}).status_code, 404)

    def test_turn_on_test_and_off(self):
        csrf = self.c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"}).json()["csrf"]
        h = {"X-CSRF-Token": csrf}
        self.assertEqual(self.c.get("/api/push").json()["devices"], [])
        dev = self.c.post("/api/push/subscribe", json={"subscription": SUB, "label": "Phone"}, headers=h).json()
        self.assertEqual((dev["label"], dev["username"]), ("Phone", "owner"))
        self.assertNotIn("endpoint", self.c.get("/api/push").json()["devices"][0])     # the endpoint stays on the server
        with mock.patch("pywebpush.webpush") as webpush:
            self.assertEqual(self.c.post("/api/push/test", json={"id": dev["id"]}, headers=h).json()["sent"], 1)
        self.assertEqual(webpush.call_count, 1)
        self.assertEqual(self.c.post("/api/push/unsubscribe", json={"id": dev["id"]}, headers=h).json(), {"removed": True})
        self.assertEqual(self.c.post("/api/push/test", json={}, headers=h).status_code, 404)

    def test_the_service_worker_is_at_the_root(self):
        r = self.c.get("/sw.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("javascript", r.headers["content-type"])
        self.assertIn('addEventListener("push"', r.text)


    def test_everything_the_worker_keeps_for_offline_is_served(self):
        d = self.c.get("/sw-precache.json").json()
        self.assertIn(f"/static/v/{d['version']}/js/main.js", d["files"])
        self.assertIn(f"/static/v/{d['version']}/css/app.css", d["files"])
        page = self.c.get("/").text
        self.assertIn(f"/static/v/{d['version']}/", page)             # the version sw.js reads from the page
        for f in d["files"]:
            self.assertEqual(self.c.get(f).status_code, 200, f)

    def test_the_icons_browsers_look_for_at_the_root_are_images(self):
        for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/apple-touch-icon-180x180.png",
                     "/apple-touch-icon-120x120-precomposed.png", "/favicon.ico"):
            for r in (self.c.get(path), self.c.head(path)):
                self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/png"), path)
        self.assertEqual(self.c.get("/no-such-icon.png").status_code, 404)        # not the page
        self.assertEqual(self.c.get("/apple-touch-icon-big.png").status_code, 404)
        self.assertIn("<!doctype html>", self.c.get("/weather").text)              # other addresses still are
        self.assertEqual(self.c.head("/weather").status_code, 200)

    def test_the_page_carries_its_icons_and_the_manifest_links_served_ones(self):
        # Inlined: iOS saves a linked touch icon by fetching it again, and got a letter tile instead (#72).
        page = self.c.get("/").text
        for rel, name in (("apple-touch-icon", "apple-touch-icon.png"), ("icon", "favicon-32.png")):
            uri = re.search(rf'<link rel="{rel}"[^>]* href="data:image/png;base64,([^"]+)"', page)
            self.assertIsNotNone(uri, rel)
            self.assertEqual(base64.b64decode(uri[1]), (STATIC / "img" / name).read_bytes(), rel)
        for u in [i["src"] for i in self.c.get("/static/manifest.webmanifest").json()["icons"]]:
            r = self.c.get(u)
            self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/png"), u)

if __name__ == "__main__":
    unittest.main()
