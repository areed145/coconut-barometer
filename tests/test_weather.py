"""WeatherFlow: obs_st parsing, storage, REST gap-fill, the stream's messages, and the Weather API."""
import os
import tempfile
import time
import unittest
from collections import deque
from datetime import datetime

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, fake_geocoder, CFG
from coconut_barometer.config import from_dict
from coconut_barometer.store import Store
from coconut_barometer.weather import sync as wxsync
from coconut_barometer.store.weather import clear_sky, local_midnight
from coconut_barometer.weather.parse import obs_st, sea_level
import json
import threading

from coconut_barometer.weather.stream import Hub, WeatherStream, handle
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import hash_password

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

    def test_dew_point_and_lowest_cloud_base(self):
        self.store.wx_save_obs(11, [arr(NOW, temp=20.0), arr(NOW + 60, temp=10.0)])     # both at 50% humidity
        d = self.store.wx_history([11], NOW - 100 * DAY, NOW + 3600)["devices"][11]
        self.assertAlmostEqual(d["dew_point"][0], (9.27 + 0.03) / 2, delta=0.05)
        self.assertAlmostEqual(d["cloud_base"][0], (10.0 - 0.03) * 125, delta=10)   # the lower of the two

    def test_feels_like_gustiness_and_clear_sky(self):
        self.store.wx_save_obs(11, [arr(NOW, temp=30.0), arr(NOW + 3 * DAY, temp=0.0), arr(NOW + 6 * DAY, temp=15.0)])
        h = self.store.wx_history([11], NOW - 100 * DAY, NOW + 7 * DAY)
        d = h["devices"][11]
        feels = [v for v in d["feels_like"] if v is not None]
        self.assertAlmostEqual(feels[0], 31.05, delta=0.1)         # heat index, 30 °C at 50% (88 °F in the NWS table)
        self.assertAlmostEqual(feels[1], -3.5, delta=0.2)          # wind chill, 0 °C in 3 m/s
        self.assertEqual(feels[2], 15.0)
        self.assertAlmostEqual([v for v in d["gust_factor"] if v][0], 5.0 / 3.0)
        self.assertEqual(len(d["clear_sky"]), len(h["t"]))
        # Austin in mid-November: around 675 W/m² near solar noon, nothing at night.
        self.assertAlmostEqual(clear_sky(30.27, -97.74, NOW - 3.5 * 3600), 675, delta=40)
        self.assertEqual(clear_sky(30.27, -97.74, NOW + 8 * 3600), 0.0)

    def test_wind_rose_by_part_of_the_local_day(self):
        # NOW is 16:13 in Chicago (afternoon); three hours later is evening.
        self.store.wx_save_obs(11, [arr(NOW), arr(NOW + 3 * 3600)])
        self.assertEqual(self.store.wx_wind_rose(11, NOW - 1, NOW + DAY, "afternoon")["total"], 1)
        self.assertEqual(self.store.wx_wind_rose(11, NOW - 1, NOW + DAY, "evening")["total"], 1)
        self.assertEqual(self.store.wx_wind_rose(11, NOW - 1, NOW + DAY, "night")["total"], 0)

    def test_wind_direction_averages_around_north(self):
        self.store.wx_save_obs(11, [arr(NOW, direction=350.0), arr(NOW + 60, direction=10.0)])
        d = self.store.wx_history([11], NOW - 100 * DAY, NOW + 3600)["devices"][11]["wind_dir"]
        self.assertTrue(d[0] < 1 or d[0] > 359)

    def test_days_group_by_local_day_and_hour(self):
        # NOW is 2023-11-14 22:13 UTC = 16:13 in Chicago; 10 h of readings cross local midnight.
        self.store.wx_save_obs(11, [arr(NOW + i * 600, temp=float(i)) for i in range(60)])
        d = self.store.wx_days(11, NOW - 1, NOW + DAY)
        self.assertEqual(d["timezone"], "America/Chicago")
        self.assertEqual([x["date"] for x in d["days"]], ["2023-11-14", "2023-11-15"])
        first, second = d["days"]
        self.assertEqual((first["low"], first["high"], second["high"]), (0.0, 46.0, 59.0))
        self.assertIsNone(first["hours"][15])
        self.assertAlmostEqual(first["hours"][16], 2.0)          # 16:13–16:53 -> 0..4
        self.assertEqual(second["t"], local_midnight("America/Chicago", NOW + DAY))
        self.assertEqual((first["rain"], first["uv"]), (0.0, 2.0))

    def test_records_all_time_and_by_month(self):
        def obs(ts, temp=20.0, gust=5.0, rain=0.0, strike_km=0, strikes=0):
            a = arr(ts, temp=temp, rain=rain)
            a[3], a[14], a[15] = gust, strike_km, strikes
            return a
        october = NOW - 30 * DAY                                     # 2023-10-15, local
        self.store.wx_save_obs(11, [
            obs(october, temp=35.0, gust=9.0),
            obs(NOW, temp=10.0), obs(NOW + 60, temp=12.0, gust=14.0, rain=2.0), obs(NOW + 120, rain=3.0),
            obs(NOW + 3600, rain=4.0),                               # same local day, the next hour
            obs(NOW + 7200, strike_km=0, strikes=2), obs(NOW + 7260, strike_km=8, strikes=1)])   # 0 km: no distance
        r = self.store.wx_records(11, now=NOW + 7300)
        rec = lambda k: (r["all"][k]["v"], r["all"][k]["ts"])
        self.assertEqual(rec("temp_high"), (35.0, october))
        self.assertEqual(rec("temp_low"), (10.0, NOW))
        self.assertEqual(rec("gust"), (14.0, NOW + 60))
        self.assertEqual(r["all"]["rain_hour"]["v"], 5.0)                      # 2 + 3 in one clock hour
        self.assertEqual(r["all"]["rain_day"], {"v": 9.0, "ts": local_midnight("America/Chicago", NOW)})
        self.assertEqual(rec("lightning"), (8, NOW + 7260))
        self.assertEqual([m["month"] for m in r["months"]], [10, 11])
        self.assertEqual(r["months"][1]["temp_high"]["v"], 20.0)               # November's own high
        self.assertEqual((r["since"], r["days"]), ((october // 3600) * 3600, 2))
        self.assertEqual([d["date"] for d in r["this_date"]], ["2023-11-14"])

    def test_highlights_streaks_and_month_so_far(self):
        def obs(ts, temp=20.0, rain=0.0, strike_km=0, strikes=0):
            a = arr(ts, temp=temp, rain=rain)
            a[14], a[15] = strike_km, strikes
            return a
        # NOW is 2023-11-14 16:13 in Chicago. Rain on Nov 10 (and a trace on the 12th that doesn't count).
        self.store.wx_save_obs(11, [
            obs(NOW - 40 * DAY, temp=39.0),                            # Oct 5: a 100 °F day (102 °F)
            obs(NOW - 4 * DAY, rain=1.5), obs(NOW - 2 * DAY, rain=0.1),
            obs(NOW - DAY, temp=30.0, strike_km=12, strikes=3),         # within 10 miles
            obs(NOW - 600, temp=-1.0, strike_km=30, strikes=5), obs(NOW, temp=15.0)])
        h = self.store.wx_highlights(11, now=NOW + 5 * DAY)           # the station stopped: as of its last reading
        self.assertEqual((h["as_of"], h["today"]), (NOW, "2023-11-14"))
        self.assertEqual(h["rain"], {"last_day": "2023-11-10", "last_amount": 1.5, "days_since": 4, "today": 0.0})
        m = h["month"]
        self.assertAlmostEqual(m["rain"], 1.6)
        self.assertEqual((m["high"]["v"], m["low"]["v"], m["strikes"]), (30.0, -1.0, 8))
        self.assertEqual((h["year"]["hot_days"], h["year"]["freezing_nights"]), (1, 1))
        self.assertEqual(h["lightning"]["last_near"], {"ts": NOW - DAY, "km": 12})     # the 30 km one is too far
        self.assertEqual(h["record"], [])                                               # under a year of history

    def test_almanac_matches_the_published_times(self):
        from coconut_barometer.store.weather import almanac
        from zoneinfo import ZoneInfo
        a = almanac(30.27, -97.74, ZoneInfo("America/Chicago"), NOW)          # Austin, 14 Nov 2023
        local = lambda ts: datetime.fromtimestamp(ts, ZoneInfo("America/Chicago"))
        rise, set_ = local(a["sunrise"]), local(a["sunset"])
        minutes = lambda t: t.hour * 60 + t.minute + t.second / 60
        self.assertLess(abs(minutes(rise) - (6 * 60 + 55.5)), 2)       # NOAA's solar calculator: 6:55:30 am CST
        self.assertLess(abs(minutes(set_) - (17 * 60 + 35.1)), 2)      # 5:35:06 pm
        self.assertEqual(a["day_length"], a["sunset"] - a["sunrise"])
        self.assertLess(a["change"], 0)                                          # November: days getting shorter

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
        from coconut_barometer.weather.service import WeatherService
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

    def test_public_but_sync_needs_login(self):
        self.assertEqual(self.c.get("/api/wx/live").status_code, 200)
        self.assertEqual(self.c.post("/api/wx/sync").status_code, 401)

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
        self.assertEqual(len(self.c.get("/api/wx/days", params={"device": 11, "hours": 2}).json()["days"]), 1)
        self.assertEqual(self.c.get("/api/wx/rose", params={"device": 11, "part": "noon"}).status_code, 422)

    def test_live_is_cached_until_something_new_is_stored(self):
        from coconut_barometer.store import weather as wx_store
        first = self.c.get("/api/wx/live").json()["stations"][0]["obs"]
        calls = []
        orig = wx_store.WeatherStore.wx_latest
        wx_store.WeatherStore.wx_latest = lambda db, d: calls.append(d) or orig(db, d)
        self.addCleanup(setattr, wx_store.WeatherStore, "wx_latest", orig)
        self.assertEqual(self.c.get("/api/wx/live").json()["stations"][0]["obs"], first)
        self.assertEqual(calls, [])                                       # nothing new: from the cache
        store = Store(self.cfg["database"], migrate=False)
        store.wx_save_obs(11, [arr(first["ts"] + 60, temp=25.0)])
        store.close()
        self.assertEqual(self.c.get("/api/wx/live").json()["stations"][0]["obs"]["temp"], 25.0)   # shown at once
        self.assertEqual(calls, [11])

    def test_highlights_are_public_and_position_free(self):
        r = self.c.get("/api/highlights")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["weather"]["name"], "Backyard")
        self.assertEqual(d["photos"]["taken"], 0)                     # no photos in this library
        self.assertIsNone(d["igate"])                                  # no igate set
        self.assertNotRegex(r.text, r'"(lat|lon|latitude|longitude|serial)"')

    def test_records_api(self):
        r = self.c.get("/api/wx/records", params={"device": 11}).json()           # public, like the other weather pages
        self.assertEqual(r["all"]["temp_high"]["v"], 20.0)
        self.assertEqual(r["units"], "metric")
        self.assertIsNotNone(r["almanac"]["sunrise"])
        self.assertEqual(self.c.get("/api/wx/records", params={"device": 99}).status_code, 404)

    def test_map_lists_stations_and_frames(self):
        from coconut_barometer.web.api import weather as wx_api
        from coconut_barometer.weather.imagery import Imagery
        from test_weather_map import FakeGet
        self.app.dependency_overrides[wx_api.imagery] = lambda: Imagery(get=FakeGet(), clock=lambda: 1_792_620_000)
        self.login()
        m = self.c.get("/api/wx/map").json()
        self.assertEqual(m["stations"], [{"device_id": 11, "name": "Backyard", "lat": 30.27, "lon": -97.74}])
        self.assertNotIn("radar", m)                 # the radar is /api/wx/radar's (weather/radar.py)
        self.assertTrue(m["satellite"]["geocolor"]["frames"])

    def test_map_errors_are_logged_and_reported_only_from_a_signed_in_browser(self):
        from coconut_barometer.errors import fingerprint
        from coconut_barometer.web.api import weather as wx_api

        self.addCleanup(setattr, wx_api, "_map_error_throttle", wx_api._map_error_throttle)
        wx_api._map_error_throttle = wx_api.MapErrorThrottle(per_client=3, global_limit=10)
        seen = []
        self.addCleanup(setattr, wx_api, "caught", wx_api.caught)
        wx_api.caught = lambda error, where: seen.append((error, where))
        stack = "TypeError: draw failed\n    at draw (https://testserver/static/v/{v}/js/radar_layer.js:{line}:2)"
        report = lambda v="abc123", line=12, **kw: self.c.post("/api/wx/map/errors", json={
            "source": "unhandledrejection", "name": "TypeError", "message": "draw failed", "stack": stack.format(v=v, line=line), **kw})
        with self.assertLogs("coconut_barometer.web.api.weather", "WARNING"):
            self.assertEqual(report().json(), {"reported": False})                # a visitor's: logged, not reported
        self.assertEqual(seen, [])
        self.login()
        self.assertEqual(report().json(), {"reported": True})
        self.assertEqual(seen[0][1], "weather map unhandledrejection")
        self.assertIn("draw failed", str(seen[0][0]))
        report(v="def456", line=40)                                            # the same bug after a deploy
        self.assertEqual(fingerprint(seen[0][0]), fingerprint(seen[1][0]))
        self.assertEqual(report().status_code, 429)                             # 3 a minute from one address
        self.assertEqual(self.c.post("/api/wx/map/errors", json={"source": "error", "message": "x" * 501}).status_code, 422)

    def test_map_error_throttle_has_a_global_cap(self):
        from coconut_barometer.web.api.weather import MapErrorThrottle

        throttle = MapErrorThrottle(per_client=5, global_limit=1)
        self.assertTrue(throttle.allow("client-one"))
        self.assertFalse(throttle.allow("client-two"))

    def test_sync_is_queued_on_the_weather_lane_and_fails_cleanly_without_network(self):
        self.login()
        csrf = {"X-CSRF-Token": self.c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"}).json()["csrf"]}
        rt = self.app.state.runtime
        rt.weather._client = FakeClient(begins=NOW)
        rt.weather._client.token = "tok"
        self.assertEqual(self.c.post("/api/wx/sync", headers=csrf).json(), {"status": "queued"})
        rt.review.wait()
        q = self.c.get("/api/status/jobs").json()
        self.assertEqual(q["recent"][0]["kind"], "wx")
        self.assertEqual(q["recent"][0]["status"], "done")
        self.assertTrue(q["services"]["weatherflow"]["configured"])


if __name__ == "__main__":
    unittest.main()
