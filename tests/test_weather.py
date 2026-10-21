"""WeatherFlow: obs_st parsing, storage, REST gap-fill, the stream's messages, and the Weather API."""
import os
import tempfile
import time
import unittest
from collections import deque

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, fake_geocoder, CFG
from flickr_organizer.config import from_dict
from flickr_organizer.store import Store
from flickr_organizer.weather import sync as wxsync
from flickr_organizer.weather.parse import obs_st, sea_level
import json
import threading

from flickr_organizer.weather.stream import Hub, WeatherStream, handle
from flickr_organizer.web.app import create_app
from flickr_organizer.web.security import hash_password

DAY = 86400
NOW = 1_700_000_000
STATION = {"station_id": 7, "name": "Backyard", "latitude": 30.27, "longitude": -97.74, "timezone": "America/Chicago",
           "station_meta": {"elevation": 150.0},
           "devices": [{"device_id": 11, "device_type": "ST", "serial_number": "ST-1"},
                       {"device_id": 12, "device_type": "HB", "serial_number": "HB-1"}]}


def arr(ts, temp=20.0, rain=0.0, wind=3.0, direction=90.0):
    return [ts, 1.0, wind, 5.0, direction, 3, 1000.0, temp, 50.0, 10000, 2.0, 300.0, rain, 0, 0, 0, 2.7, 1]


class FakeClient:
    """Serves one day of 1-minute data per request, but only for days on or after `begins`."""
    def __init__(self, begins):
        self.begins, self.call_count, self.log, self.token = begins, 0, deque(maxlen=50), "t"
        self.requests = []

    def stations(self):
        self.call_count += 1
        return [STATION]

    def device_obs(self, device_id, start, end):
        self.call_count += 1
        self.requests.append((device_id, start, end))
        return [arr(t) for t in range(max(start, self.begins), end + 1, 60)] if end >= self.begins else []


class ParseTest(unittest.TestCase):
    def test_obs_st_is_named_and_short_arrays_are_dropped(self):
        o = obs_st(arr(NOW, temp=21.5, wind=4.2))
        self.assertEqual((o["ts"], o["temp"], o["wind_avg"], o["humidity"], o["battery"]), (NOW, 21.5, 4.2, 50.0, 2.7))
        self.assertIsNone(obs_st([NOW, 1, 2]))
        self.assertIsNone(obs_st([None] + [0] * 20))

    def test_sea_level_pressure_is_higher_above_sea_level(self):
        self.assertGreater(sea_level(1000.0, 15.0, 150.0), 1010)
        self.assertEqual(sea_level(1000.0, 15.0, 0), 1000.0)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))
        self.store.wx_save_stations([STATION])

    def test_only_tempests_are_listed_and_saving_twice_replaces(self):
        self.assertEqual([s["device_id"] for s in self.store.wx_stations()], [11])
        self.assertEqual(self.store.wx_save_obs(11, [arr(NOW), arr(NOW + 60)]), 2)
        self.store.wx_save_obs(11, [arr(NOW, temp=30.0)])
        self.assertEqual(self.store.wx_range(11), (NOW, NOW + 60))
        self.assertEqual(self.store.wx_stations()[0]["observations"], 2)

    def test_latest_has_day_totals_and_pressure_trend(self):
        self.store.wx_save_obs(11, [arr(NOW - 4 * 3600, temp=10.0), arr(NOW - 60, temp=25.0, rain=1.5), arr(NOW, temp=22.0, rain=0.5)])
        st = self.store.wx_latest(11)
        self.assertEqual(st["name"], "Backyard")
        o = st["obs"]
        self.assertEqual(o["ts"], NOW)
        self.assertLessEqual(o["rain_today"], 2.0)       # today's total starts at the station's midnight
        self.assertEqual(o["temp_high"] >= 22.0, True)
        self.assertIsNotNone(o["pressure_change_3h"])
        self.assertGreater(o["pressure_sl"], o["pressure"])

    def test_history_buckets_grow_with_the_range_and_align_devices(self):
        self.store.wx_save_obs(11, [arr(NOW + i * 60, temp=10.0 + i, rain=0.1) for i in range(120)])
        h = self.store.wx_history([11], NOW, NOW + 7200)
        self.assertEqual(h["bucket"], 60)
        self.assertEqual(len(h["t"]), 120)
        long = self.store.wx_history([11], NOW - 800 * DAY, NOW + 7200)
        self.assertEqual(long["bucket"], 86400)
        self.assertAlmostEqual(sum(v for v in long["devices"][11]["rain"] if v), 12.0)

    def test_wind_direction_averages_around_north(self):
        self.store.wx_save_obs(11, [arr(NOW, direction=350.0), arr(NOW + 60, direction=10.0)])
        d = self.store.wx_history([11], NOW - 100 * DAY, NOW + 3600)["devices"][11]["wind_dir"]
        self.assertTrue(d[0] < 1 or d[0] > 359)

    def test_wind_rose_counts_by_direction(self):
        self.store.wx_save_obs(11, [arr(NOW, wind=1.0, direction=0.0), arr(NOW + 60, wind=6.0, direction=90.0)])
        r = self.store.wx_wind_rose(11, NOW - 1, NOW + 100)
        self.assertEqual((r["total"], r["counts"][0][0], r["counts"][4][2]), (2, 1, 1))


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))

    def test_first_sync_fetches_a_day_per_request_for_the_backfill_window(self):
        client = FakeClient(begins=NOW - 30 * DAY)
        out = wxsync.sync(client, self.store, backfill_days=3, now=NOW)
        self.assertEqual(out["stations"], 1)
        self.assertEqual(out["calls"], 1 + 3)          # stations + 3 days
        first, last = self.store.wx_range(11)
        self.assertLessEqual(first, NOW - 3 * DAY + 60)
        self.assertGreaterEqual(last, NOW - 60)

    def test_second_sync_fetches_only_the_gap(self):
        client = FakeClient(begins=NOW - 30 * DAY)
        wxsync.sync(client, self.store, backfill_days=3, now=NOW)
        client.requests.clear()
        wxsync.sync(client, self.store, backfill_days=3, now=NOW + 3600)
        self.assertEqual(len(client.requests), 1)
        self.assertGreater(client.requests[0][1], NOW - 120)

    def test_raising_backfill_days_fills_only_older_days(self):
        client = FakeClient(begins=NOW - 30 * DAY)
        wxsync.sync(client, self.store, backfill_days=2, now=NOW)
        first = self.store.wx_range(11)[0]
        client.requests.clear()
        wxsync.sync(client, self.store, backfill_days=4, now=NOW)
        self.assertTrue(all(end < first + 60 for _, _, end in client.requests if end < NOW - 60))
        self.assertLessEqual(self.store.wx_range(11)[0], NOW - 4 * DAY + 60)

    def test_a_device_that_started_late_stops_the_walk_back(self):
        client = FakeClient(begins=NOW - 2 * DAY)
        wxsync.sync(client, self.store, backfill_days=60, now=NOW)
        self.assertLess(client.call_count, 20)
        again = client.call_count
        wxsync.sync(client, self.store, backfill_days=60, now=NOW + 60)
        self.assertLess(client.call_count - again, 4)    # stations + the new minute, not the empty past


class StreamTest(unittest.TestCase):
    def test_messages_are_stored_or_kept_live(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))
        store.wx_save_stations([STATION])
        hub = Hub()
        self.assertEqual(handle({"type": "obs_st", "device_id": 11, "obs": [arr(NOW)]}, store, hub), "obs_st")
        handle({"type": "rapid_wind", "device_id": 11, "ob": [NOW, 4.5, 270]}, store, hub)
        handle({"type": "evt_strike", "device_id": 11, "evt": [NOW, 12, 3000]}, store, hub)
        handle({"type": "evt_precip", "device_id": 11}, store, hub)
        handle({"type": "ack", "id": "x"}, store, hub)
        self.assertEqual(store.wx_range(11), (NOW, NOW))
        snap = hub.snapshot()
        self.assertEqual(snap["rapid"]["11"]["speed"], 4.5)
        self.assertEqual([e["kind"] for e in snap["events"]], ["strike", "rain_start"])
        self.assertEqual(store.wx_latest(11)["obs"]["last_strike"], {"ts": NOW, "distance": 12})


class FakeSocket:
    def __init__(self, messages, stop):
        self.messages, self.stop, self.sent, self.closed = list(messages), stop, [], False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        if not self.messages:
            self.stop.set()
            raise TimeoutError
        return json.dumps(self.messages.pop(0))

    def close(self):
        self.closed = True


class StreamThreadTest(unittest.TestCase):
    def test_subscribes_each_tempest_stores_what_arrives_and_asks_for_a_fill_on_connect(self):
        from flickr_organizer.weather.service import WeatherService
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        Store(path).wx_save_stations([STATION])
        svc = WeatherService(from_dict({**CFG, "database": path, "weatherflow": {"token": "tok", "backfill_days": 7,
                                                                                  "calls_per_second": 2.0}}), path)
        stop, filled = threading.Event(), []
        sock = FakeSocket([{"type": "ack", "id": "obs-11-0"}, {"type": "obs_st", "device_id": 11, "obs": [arr(NOW)]},
                           {"type": "rapid_wind", "device_id": 11, "ob": [int(time.time()), 2.0, 180]}], stop)
        urls = []
        t = WeatherStream(svc, stop, on_connect=lambda: filled.append(1), connect=lambda url: urls.append(url) or sock)
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertIn("token=tok", urls[0])
        self.assertEqual([m["type"] for m in sock.sent], ["listen_start", "listen_rapid_start"])
        self.assertTrue(all(m["device_id"] == 11 for m in sock.sent))
        self.assertEqual(svc.store().wx_range(11), (NOW, NOW))
        self.assertEqual(svc.hub.snapshot()["rapid"]["11"]["dir"], 180)
        self.assertEqual((len(filled), svc.hub.status["messages"], sock.closed), (1, 3, True))


class WeatherApiTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        store = Store(path)
        store.set_password_hash("owner", hash_password("correct horse battery"))
        store.wx_save_stations([STATION])
        now = int(time.time())
        store.wx_save_obs(11, [arr(now - 60 * i, temp=20.0 - i) for i in range(30)])
        self.cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]},
                              "weatherflow": {"token": "tok", "backfill_days": 7, "calls_per_second": 2.0}})
        self.app = create_app(self.cfg, FakeFlickr(), provider_factory=lambda: FakeVision("m"), scheduler=False)
        self.c = TestClient(self.app, base_url="https://testserver")

    def login(self):
        self.c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"})

    def test_needs_login(self):
        self.assertEqual(self.c.get("/api/wx/live").status_code, 401)

    def test_stations_live_history_and_rose(self):
        self.login()
        st = self.c.get("/api/wx/stations").json()
        self.assertTrue(st["configured"])
        self.assertEqual(st["stations"][0]["observations"], 30)
        live = self.c.get("/api/wx/live").json()
        self.assertEqual(live["stations"][0]["obs"]["temp"], 20.0)
        h = self.c.get("/api/wx/history", params={"device": 11, "hours": 2}).json()
        self.assertEqual(len(h["t"]), 30)
        self.assertEqual(self.c.get("/api/wx/history", params={"device": 99}).status_code, 404)
        self.assertEqual(self.c.get("/api/wx/rose", params={"device": 11}).json()["total"], 30)

    def test_sync_is_queued_on_the_weather_lane_and_fails_cleanly_without_network(self):
        self.login()
        csrf = {"X-CSRF-Token": self.c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"}).json()["csrf"]}
        rt = self.app.state.runtime
        rt.weather._client = FakeClient(begins=NOW)
        rt.weather._client.token = "tok"
        self.assertEqual(self.c.post("/api/wx/sync", headers=csrf).json(), {"status": "queued"})
        rt.review.wait()
        q = self.c.get("/api/review/worker").json()
        self.assertEqual(q["recent"][0]["kind"], "wx")
        self.assertEqual(q["recent"][0]["status"], "done")
        self.assertIn("weather", q)
        self.assertTrue(q["weather"]["configured"])


if __name__ == "__main__":
    unittest.main()
