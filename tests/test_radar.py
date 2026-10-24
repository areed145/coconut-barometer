"""Radar (weather/radar.py): an MRMS frame decoded, cropped, cleaned and traced into shapes; storm
cells read, merged and carried along their course; and the storm and rain alerts built on them."""
import calendar
import gzip
import io
import json
import math
import os
import struct
import tempfile
import time
import unittest
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from fakes import FakeFlickr, CFG
from coconut_barometer import activity, push
from coconut_barometer import settings as S
from coconut_barometer.alerts import Alerts
from coconut_barometer.config import from_dict
from coconut_barometer.store import Store
from coconut_barometer.weather import radar as R
from coconut_barometer.weather import snapshot as SN
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import hash_password

LAT, LON = 30.0, -95.5                       # the station: Houston-ish, in the middle of the test grid
STATION = {"station_id": 7, "name": "Conroe", "latitude": LAT, "longitude": LON, "timezone": "America/Chicago",
           "station_meta": {"elevation": 0}, "devices": [{"device_id": 11, "device_type": "ST", "serial_number": "ST-1"}]}
T = calendar.timegm((2026, 10, 24, 0, 20, 39, 0, 0, 0))


def grib(dbz: np.ndarray, la1=31.0, lo1=-96.5, step=0.01, t=T) -> bytes:
    """A GRIB2 message laid out as MRMS's: a lat/lon grid (template 3.0), PNG-packed (5.41), value = (raw - 9990) / 10."""
    raw = np.clip(np.round(dbz * 10 + 9990), 0, 65535).astype(np.uint16)
    png = io.BytesIO()
    Image.fromarray(raw).save(png, format="PNG")
    nj, ni = raw.shape
    y, mo, d, h, mi, se = time.gmtime(t)[:6]
    sec1 = struct.pack(">IB4xBBB", 21, 1, 2, 1, 1) + struct.pack(">H5B", y, mo, d, h, mi, se) + b"\0\0"
    sec3 = bytearray(72)
    sec3[0:5] = struct.pack(">IB", 72, 3)
    sec3[30:38] = struct.pack(">II", ni, nj)
    sec3[46:54] = struct.pack(">iI", round(la1 * 1e6), round((lo1 % 360) * 1e6))
    sec3[63:71] = struct.pack(">II", round(step * 1e6), round(step * 1e6))
    sec5 = struct.pack(">IBIH", 21, 5, ni * nj, 41) + struct.pack(">f", -9990.0) + struct.pack(">HHB", 0, 1, 16) + b"\0"
    sec7 = struct.pack(">IB", 5 + len(png.getvalue()), 7) + png.getvalue()
    body = sec1 + bytes(sec3) + sec5 + sec7 + b"7777"
    return b"GRIB\0\0\0\x02" + struct.pack(">Q", 16 + len(body)) + body


def frame_of(z: np.ndarray) -> dict:
    """A frame's shapes at both levels, as RadarService.refresh_frame makes them."""
    msg = R.decode(grib(z))
    us = R.crop(msg, raw=R.grid(msg), block=R.US_BLOCK)
    clean = R.clean(us.pop("dbz"), R.US_SPECK)
    return {"t": msg["t"], "box": (20, 55, -130, -60), "levels": {n: R.encode(us, clean, *spec) for n, spec in R.LEVELS.items()}}


def blob(west_km: float, dbz: float = 40, radius_px: int = 5) -> np.ndarray:
    """A 200×200 grid around the station with a disc of rain `west_km` west of it, and a one-pixel speck."""
    z = np.full((200, 200), -99.0)
    yy, xx = np.mgrid[0:200, 0:200]
    cx = 100 - west_km / (R.KM_PER_DEG * math.cos(math.radians(LAT)) * 0.01)
    z[(yy - 100) ** 2 + (xx - cx) ** 2 <= radius_px ** 2] = dbz
    z[20, 180] = 60                                                   # a speck: cleaned away
    return z


class FrameTest(unittest.TestCase):
    def test_decode_crop_clean_and_trace(self):
        msg = R.decode(grib(blob(20)))
        self.assertEqual((msg["t"], msg["la1"], msg["lo1"], msg["ni"], msg["nj"]), (T, 31.0, -96.5, 200, 200))
        field = R.crop(msg, LAT - 0.5, LAT + 0.5, LON - 0.5, LON + 0.5)
        self.assertEqual(field["dbz"].max(), 40.0)
        z = R.clean(R.crop(msg, LAT - 1, LAT + 1, LON - 1, LON + 1)["dbz"])
        self.assertEqual(z.max(), 40.0)                                # the 60 dBZ speck is gone
        f = R.crop(msg, LAT - 1, LAT + 1, LON - 1, LON + 1)
        self.assertAlmostEqual(R.nearest_km(f, R.clean(f.pop("dbz")), LAT, LON), 20 - 5, delta=2)


    def test_two_levels_of_stacked_shapes_and_only_what_is_in_view(self):
        frame = frame_of(blob(20, radius_px=12))
        fine = json.loads(R.view(frame, (LON - 2, LAT - 2, LON + 2, LAT + 2), 7))
        self.assertEqual((fine["level"], fine["q"]), ("fine", 1000))
        self.assertEqual([b["dbz"] for b in fine["bands"]], [15, 20, 25, 30, 35])
        boxes = [b["polys"][0][:4] for b in fine["bands"]]             # each level and up: inside the one below
        for lower, upper in zip(boxes, boxes[1:]):
            self.assertTrue(lower[0] <= upper[0] and lower[1] <= upper[1] and upper[2] <= lower[2] and upper[3] <= lower[3])
        w, s_, e, n = (x / 1000 for x in boxes[0])
        self.assertTrue(LON - 0.35 < w < e < LON - 0.05 and s_ < LAT < n)   # where the rain is, ~20 km west
        ring = fine["bands"][0]["polys"][0][4]
        x, y = np.cumsum(ring[0::2]), np.cumsum(ring[1::2])             # each point the step from the one before
        self.assertTrue((x >= boxes[0][0]).all() and (x <= boxes[0][2]).all() and (y >= boxes[0][1]).all() and (y <= boxes[0][3]).all())
        self.assertEqual(json.loads(R.view(frame, (LON + 1, LAT + 1, LON + 2, LAT + 2), 7))["bands"], [])   # nothing in view
        coarse = json.loads(R.view(frame, None, 4))
        self.assertEqual((coarse["level"], coarse["q"]), ("coarse", 100))
        self.assertLess(sum(len(r) for b in coarse["bands"] for p in b["polys"] for r in p[4:]),
                        sum(len(r) for b in fine["bands"] for p in b["polys"] for r in p[4:]))

    def test_service_fetches_only_a_new_frame_and_tracks_the_rain_closing_in(self):
        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "db.duckdb"))
        store.wx_save_stations([STATION])
        frames, asked = iter([30, 25, 20]), []

        def get(url, headers=None, timeout=None):
            asked.append(headers)
            if headers:
                return mock.Mock(status_code=304)
            return mock.Mock(status_code=200, content=gzip.compress(grib(blob(next(frames)))), raise_for_status=lambda: None,
                             headers={"Last-Modified": "Sat, 24 Oct 2026 00:20:39 GMT"})
        svc = R.RadarService({}, store.path if hasattr(store, "path") else "", get=get)
        stations = svc.stations(store)
        self.assertEqual(stations[0]["name"], "Conroe")
        self.assertTrue(svc.refresh_frame(stations))
        self.assertFalse(svc.refresh_frame(stations))                   # 304: nothing new
        self.assertEqual(asked[1], {"If-Modified-Since": "Sat, 24 Oct 2026 00:20:39 GMT"})
        svc.rain[11].clear()
        for i, km in enumerate((30, 25, 20)):                          # 5 km closer every 4 minutes: 75 km/h
            svc.rain[11].append((T + i * 240, km))
        c = svc.closing(11, T + 480)
        self.assertAlmostEqual(c["kmh"], 75, delta=1)
        self.assertEqual(c["km"], 20)

    def _service(self, responses):
        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "db.duckdb"))
        store.wx_save_stations([STATION])
        calls = []

        def get(url, headers=None, timeout=None):
            calls.append(headers)
            return responses[len(calls) - 1]
        svc = R.RadarService({}, "", get=get)
        return svc, svc.stations(store), calls

    def _ok(self):
        return mock.Mock(status_code=200, content=gzip.compress(grib(blob(20))), raise_for_status=lambda: None,
                         headers={"Last-Modified": "Sat, 24 Oct 2026 00:20:39 GMT"})

    def _cut(self):
        return mock.Mock(status_code=200, content=gzip.compress(grib(blob(20)))[:100], raise_for_status=lambda: None, headers={})

    def _details(self):
        return [e["detail"] for e in activity.log.entries["radar"] if e["kind"] == "call"]

    def test_truncated_download_is_retried_and_noted(self):
        svc, stations, calls = self._service([self._cut(), self._ok()])
        self.assertTrue(svc.refresh_frame(stations))
        self.assertEqual(len(calls), 2)
        self.assertIn("(truncated download)", self._details()[-1])
        self.assertEqual(svc._modified, "Sat, 24 Oct 2026 00:20:39 GMT")

    def test_two_truncated_downloads_return_false_and_keep_the_cache(self):
        svc, stations, calls = self._service([self._cut(), self._cut()])
        svc._modified = "old"
        self.assertFalse(svc.refresh_frame(stations))
        self.assertEqual(len(calls), 2)
        self.assertIsNone(svc.frame)
        self.assertEqual(svc._modified, "old")
        self.assertIn("(truncated download)", self._details()[-1])

    def test_retry_returning_304_keeps_the_truncation_note(self):
        svc, stations, calls = self._service([self._cut(), mock.Mock(status_code=304)])
        svc._modified = "old"
        self.assertFalse(svc.refresh_frame(stations))
        self.assertEqual(len(calls), 2)
        self.assertEqual(svc._modified, "old")
        self.assertIn("(truncated download)", self._details()[-1])

    def test_retry_http_error_raises(self):
        def boom():
            raise OSError("503")
        svc, stations, calls = self._service([self._cut(), mock.Mock(status_code=503, raise_for_status=boom)])
        with self.assertRaises(OSError):
            svc.refresh_frame(stations)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(svc.frame)


class CellsTest(unittest.TestCase):
    def cell(self, west_km=30, toward=90, kmh=60, valid=T, dbz=55, **kw):
        return {"id": "HGX-A1", "lat": LAT, "lon": LON - west_km / (R.KM_PER_DEG * math.cos(math.radians(LAT))),
                "valid": valid, "toward": toward, "kmh": kmh, "dbz": dbz, "posh": 0, "poh": 0, "tvs": False, **kw}

    def test_iem_motion_is_where_it_comes_from(self):
        cells = R.cells_from({"features": [{"properties": {"nexrad": "HGX", "storm_id": "A1", "drct": 270, "sknt": 20,
                                                           "max_dbz": 55, "valid": "2026-10-24T00:20:39Z"},
                                            "geometry": {"type": "Point", "coordinates": [LON, LAT]}}]})
        self.assertEqual((cells[0]["toward"], round(cells[0]["kmh"], 1), cells[0]["valid"]), (90, 37.0, T))

    def test_refresh_keeps_storm_cells_beyond_station_range(self):
        geojson = {"features": [{"properties": {"nexrad": "OKX", "storm_id": "A1", "valid": "2026-10-24T00:20:39Z"},
                                 "geometry": {"type": "Point", "coordinates": [-74.0, 40.7]}}]}
        response = mock.Mock(json=lambda: geojson, raise_for_status=lambda: None)
        svc = R.RadarService({}, "", get=lambda *args, **kwargs: response, clock=lambda: T)

        svc.refresh_cells([{"lat": LAT, "lon": LON}])

        self.assertEqual([c["id"] for c in svc.snapshot()["cells"]], ["OKX-A1"])

    def test_arrival_along_its_course(self):
        self.assertAlmostEqual(R.arrival(self.cell(), LAT, LON, T, 10, 3600), 20 * 60, delta=5)   # 20 km at 60 km/h
        self.assertIsNone(R.arrival(self.cell(toward=270), LAT, LON, T, 10, 3600))               # heading away
        self.assertIsNone(R.arrival(self.cell(), LAT, LON, T, 10, 600))                          # not within 10 min
        self.assertEqual(R.arrival(self.cell(west_km=5), LAT, LON, T, 10, 3600), 0)              # already here
        self.assertAlmostEqual(R.arrival(self.cell(valid=T - 600), LAT, LON, T, 10, 3600), 10 * 60, delta=5)   # scanned 10 min ago

    def test_one_storm_seen_by_two_radars_is_one_cell(self):
        a, b = self.cell(dbz=50), {**self.cell(dbz=58, posh=40), "id": "LCH-B2", "valid": T - 120}
        merged = R.merge([a, b, self.cell(west_km=120)], T)
        self.assertEqual(len(merged), 2)
        self.assertEqual((merged[0]["id"], merged[0]["dbz"], merged[0]["posh"]), ("HGX-A1", 58, 40))


class RadarAlertsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        patch = mock.patch.object(S, "DEFAULT_KEY_FILE", os.path.join(tmp, "secret.key"))
        patch.start()
        self.addCleanup(patch.stop)
        self.path = os.path.join(tmp, "db.duckdb")
        self.store = Store(self.path)
        self.store.wx_save_stations([STATION])
        self.cfg = from_dict({**CFG, "database": self.path})
        self.radar = R.RadarService(self.cfg, self.path)
        self.alerts = Alerts(self.cfg, radar=self.radar)
        self.alerts.since = T - 60

    def by_kind(self):
        return {a["key"].split(":")[0]: a for a in self.alerts.check(self.store, T)}

    def storm(self, **kw):
        """A cell whose motion has held for two scans: what can alert."""
        return CellsTest.cell(None, steady=2, track="HGX-A1", **kw)

    def test_a_storm_on_its_way(self):
        self.radar.cells = [self.storm(posh=40)]
        a = self.by_kind()["storm"]
        self.assertEqual((a["title"], a["why"]), ("Storm ~20 min from Conroe", None))
        self.assertEqual(a["body"], "55 dBZ, 30 km W, moving E at 60 km/h; hail likely (40% severe).")
        self.radar.cells = [self.storm(dbz=40)]                          # under alerts.storm_dbz
        self.assertNotIn("storm", self.by_kind())

    def test_storm_guards(self):
        """#71: a cell seen once, or moving implausibly fast, is held back (with why); no speed isn't projected."""
        self.radar.cells = [CellsTest.cell(None)]                        # one scan
        self.assertIn("one scan", self.by_kind()["storm"]["why"])
        self.radar.cells = [self.storm(west_km=200, kmh=250)]            # 250 km/h: a tracking error
        self.assertIn("faster than 120 km/h", self.by_kind()["storm"]["why"])
        self.radar.cells = [self.storm(kmh=0)]                           # 30 km off and not moving: nothing
        self.assertNotIn("storm", self.by_kind())
        self.radar.cells = [self.storm(west_km=5, kmh=0)]                # but overhead it is, moving or not
        self.assertEqual((self.by_kind()["storm"]["title"], self.by_kind()["storm"]["why"]), ("Storm over Conroe", None))
        self.radar.cells = [CellsTest.cell(None), self.storm(west_km=40)]   # the soonest believable one, not the soonest
        a = self.by_kind()["storm"]
        self.assertEqual((a["why"], a["evidence"]["distance_km"]), (None, 40.0))

    def test_a_storm_keeps_its_track_across_radars_and_a_jump_starts_over(self):
        first = R.track([CellsTest.cell(None)], [], T)
        self.assertEqual((first[0]["track"], first[0]["steady"]), ("HGX-A1", 1))
        moved = {**CellsTest.cell(None, west_km=25, valid=T + 300), "id": "LCH-B7"}   # 5 km east in 5 min: 60 km/h, as reported
        second = R.track([moved], first, T + 300)
        self.assertEqual((second[0]["track"], second[0]["steady"], round(second[0]["obs_kmh"])), ("HGX-A1", 2, 60))
        jumped = {**CellsTest.cell(None, west_km=28, valid=T + 600, toward=270, kmh=90), "id": "LCH-B7"}   # turned round
        self.assertEqual(R.track([jumped], second, T + 600)[0]["steady"], 1)
        again = R.track([dict(second[0])], second, T + 300)              # the same scan again: as it was
        self.assertEqual(again[0]["steady"], 2)

    def rain(self, *kms):
        self.radar.rain[11] = R.deque(((T - 240 * (len(kms) - 1) + i * 240, km) for i, km in enumerate(kms)), maxlen=20)

    def test_rain_closing_in(self):
        self.rain(30, 25, 20)
        a = self.by_kind()["rain"]
        self.assertEqual((a["title"], a["why"]), ("Rain ~14 min from Conroe", None))   # (20 − 3) km at 75 km/h
        self.assertEqual(a["body"], "20 km away, closing at 75 km/h.")
        self.cfg["alerts"]["rain_minutes"] = 10
        self.assertNotIn("rain", self.by_kind())

    def test_rain_guards(self):
        self.rain(40, 40, 39, 12)                                        # a new echo forming nearer
        self.assertIn("new echo", self.by_kind()["rain"]["why"])
        self.cfg["alerts"]["rain_minutes"] = 60
        self.rain(30, 24, 29, 23, 28, 21, 18)                            # coming and going: not a steady approach
        self.assertIn("not a steady approach", self.by_kind()["rain"]["why"])

    def send_rounds(self):
        push.subscribe(self.store, {"endpoint": "https://push.example/1", "keys": {"p256dh": "k", "auth": "a"}}, "owner", "Phone", "")
        sent = mock.patch.object(push, "send", return_value={"sent": 1, "failed": [], "removed": 0})
        sent.start()
        self.addCleanup(sent.stop)
        return lambda at: [x["key"] for x in self.alerts.run_once(self.store, at)]

    def test_one_alert_per_storm_unless_it_gets_worse(self):
        run = self.send_rounds()
        self.radar.cells = [self.storm()]
        self.assertEqual(run(T), ["storm:11:HGX-A1"])
        self.assertEqual(run(T + 60), [])                                # the same storm: held back, noted once
        self.assertEqual(run(T + 120), [])
        held = [x for x in self.store.alert_log_recent() if x["status"] == "suppressed"]
        self.assertEqual(len(held), 1)
        self.assertIn("cool-down", held[0]["detail"])
        self.radar.cells = [{**self.storm(dbz=45, west_km=20), "track": "HGX-Z9"}]   # a new, weaker storm: no
        self.assertEqual(run(T + 180), [])
        self.radar.cells = [self.storm(dbz=60, posh=40)]                 # worse news: through the cool-down
        self.assertEqual(run(T + 240), ["storm:11:HGX-A1"])

    def test_at_most_so_many_radar_alerts_an_hour(self):
        run = self.send_rounds()
        self.cfg["alerts"].update(radar_per_hour=2, cooldown_minutes=5)
        for i in range(3):
            self.radar.cells = [self.storm(dbz=55 + 10 * i)]             # worse every time: only the limit stops it
            run(T + 600 * i)
        held = [x for x in self.store.alert_log_recent() if x["status"] == "suppressed"]
        self.assertEqual(len([x for x in self.store.alert_log_recent() if x["status"] == "sent"]), 2)
        self.assertIn("last hour", held[0]["detail"])


class RadarApiTest(unittest.TestCase):
    def test_the_map_gets_shapes_colors_and_cells(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]}})
        app = create_app(cfg, FakeFlickr(), scheduler=False)
        rt = app.state.runtime
        rt.radar.frame = frame_of(blob(20, radius_px=12))
        rt.radar.cells = [CellsTest.cell(None), {**CellsTest.cell(None), "id": "OKX-B1", "lat": 40.7, "lon": -74.0}]
        c = TestClient(app, base_url="https://testserver")
        d = c.get(f"/api/wx/radar?zoom=7&bbox={LON - 2},{LAT - 2},{LON + 2},{LAT + 2}").json()
        self.assertEqual((d["t"], d["shapes"]["level"], d["colors"][0]), (T, "fine", {"dbz": 15, "color": "#8fd3ff"}))
        self.assertEqual(len(d["shapes"]["bands"]), 5)
        self.assertEqual([x["id"] for x in d["cells"]], ["HGX-A1"])                   # only the cells near the view
        self.assertEqual(len(d["cells"][0]["path"]), 3)
        self.assertGreater(d["cells"][0]["path"][2][1], d["cells"][0]["path"][0][1])   # heading east
        whole = c.get("/api/wx/radar?zoom=4").json()                                   # zoomed out: the US, coarse
        self.assertEqual((whole["shapes"]["level"], len(whole["cells"])), ("coarse", 2))
        self.assertEqual(c.get("/api/wx/radar?bbox=1,2,3").status_code, 422)

    def test_the_loop_keeps_frames_with_their_storm_cells_and_deletes_old_ones(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]}})
        app = create_app(cfg, FakeFlickr(), scheduler=False)
        svc = app.state.runtime.radar
        svc.cells = [CellsTest.cell(None)]
        for t in (T - R.LOOP_WINDOW - 60, T - 120, T):
            svc._save({**frame_of(blob(20)), "t": t})
        self.assertEqual(svc.history(), [T - 120, T])                       # older than LOOP_WINDOW: deleted
        c = TestClient(app, base_url="https://testserver")
        self.assertEqual(c.get("/api/wx/radar/frames").json(), {"frames": [{"t": T - 120}, {"t": T}]})
        d = c.get(f"/api/wx/radar/frame?t={T}&zoom=7&bbox={LON - 2},{LAT - 2},{LON + 2},{LAT + 2}").json()
        self.assertEqual((d["t"], len(d["cells"]), len(d["cells"][0]["path"])), (T, 1, 3))
        self.assertEqual((d["shapes"]["level"], d["shapes"]["bands"][0]["dbz"]), ("fine", 15))
        self.assertIs(svc.stored(T), svc.stored(T))                                    # read once, then from memory
        self.assertEqual(c.get("/api/wx/radar/frame?t=1").status_code, 404)



class RadarErrorsTest(unittest.TestCase):
    def test_a_truncated_frame_is_reported_until_a_good_round(self):
        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "db.duckdb"))
        store.wx_save_stations([STATION])
        good, now = [False], [T]
        whole = gzip.compress(grib(blob(20)))
        cut = whole[:len(whole) // 2]

        def get(url, headers=None, timeout=None):
            if "nexrad_attr" in url:
                return mock.Mock(status_code=200, raise_for_status=lambda: None, json=lambda: {"features": []})
            body = gzip.compress(grib(blob(20))) if good[0] else cut
            return mock.Mock(status_code=200, content=body, raise_for_status=lambda: None, headers={})
        svc = R.RadarService({}, "", get=get, clock=lambda: now[0])
        svc.tick(store)                                                 # frame truncated twice; cells fine
        self.assertIn("truncated", svc.snapshot()["error"])
        good[0], now[0] = True, T + R.FRAME_EVERY
        svc.tick(store)
        self.assertIsNone(svc.snapshot()["error"])

if __name__ == "__main__":
    unittest.main()


class SnapshotTest(unittest.TestCase):
    """Home's Radar card (#77): a PNG of the newest radar around the stations (weather/snapshot.py)."""
    STOPS = [{"dbz": 15, "color": "#8fd3ff"}, {"dbz": 25, "color": "#2fa84f"}, {"dbz": 35, "color": "#f2d22e"},
             {"dbz": 45, "color": "#f28c28"}, {"dbz": 55, "color": "#d7301f"}, {"dbz": 65, "color": "#b03ab5"}]

    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        store = Store(path)
        store.set_password_hash("owner", hash_password("correct horse battery"))
        store.wx_save_stations([STATION])
        cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]}})
        self.app = create_app(cfg, FakeFlickr(), scheduler=False)
        self.rt = self.app.state.runtime
        self.c = TestClient(self.app, base_url="https://testserver")

    def field(self, z, t=None):
        msg = R.decode(grib(z))
        f = R.crop(msg, LAT - 1, LAT + 1, LON - 1, LON + 1)
        return {**f, "t": t or int(time.time()), "z": R.clean(f.pop("dbz"))}

    def test_colors_are_the_maps(self):
        # radar_layer.js: a level in the color at its floor, blended between the stops (Math.round)
        self.assertEqual(SN.level_color(self.STOPS, 40), (0xf2, 0xaf, 0x2b))      # halfway from #f2d22e to #f28c28
        self.assertEqual(SN.level_color(self.STOPS, 44.9), SN.level_color(self.STOPS, 40))
        self.assertEqual(SN.level_color(self.STOPS, 45), (0xf2, 0x8c, 0x28))      # on a stop: its own color
        self.assertEqual(SN.level_color(self.STOPS, 3), (0x8f, 0xd3, 0xff))        # below the first: the first
        self.assertEqual(SN.level_color(self.STOPS, 80), (0xb0, 0x3a, 0xb5))
        self.assertIsNone(SN.level_color(self.STOPS, None))

    def test_pixels_at_a_known_dbz(self):
        field = self.field(blob(0, dbz=42, radius_px=30))                          # rain right over the station
        box = SN.Box.around([(LAT, LON)])
        rgba = SN.radar_rgba(field, box, self.STOPS)
        x, y = (round(v) for v in box.px(LAT, LON))
        self.assertEqual(tuple(rgba[y, x]), (*SN.level_color(self.STOPS, 40), round(0.85 * 255)))
        self.assertEqual(rgba[5, 5, 3], 0)                                          # no echo: clear
        png = Image.open(io.BytesIO(SN.render(box, "dark", self.STOPS, field, [], [], None)))
        r, g, b = png.getpixel((x, y + 40))                                          # over the plain background, at 85%
        want = [round(c * 0.85 + bg * 0.15) for c, bg in zip(SN.level_color(self.STOPS, 40), SN.THEMES["dark"]["bg"])]
        for got, w in zip((r, g, b), want):
            self.assertAlmostEqual(got, w, delta=2)

    def test_endpoint_needs_a_login_and_renders_once_per_frame(self):
        self.assertEqual(self.c.get("/api/wx/radar/snapshot.png").status_code, 401)
        self.assertEqual(self.c.get("/api/wx/radar/glance").status_code, 401)
        self.c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"})
        self.rt.radar.field = self.field(blob(20, radius_px=12))
        self.rt.radar.cells = [CellsTest.cell(None, valid=int(time.time()))]
        r = self.c.get("/api/wx/radar/snapshot.png?theme=light")
        self.assertEqual((r.status_code, r.headers["content-type"], r.headers["cache-control"]), (200, "image/png", "private, max-age=120"))
        self.assertEqual(Image.open(io.BytesIO(r.content)).size, SN.SIZE)
        self.assertEqual(Image.open(io.BytesIO(self.c.get("/api/wx/radar/snapshot.png?scale=2").content)).size, (1280, 720))
        renders = self.rt.snapshots.renders
        again = self.c.get("/api/wx/radar/snapshot.png?theme=light&t=123")
        self.assertEqual((again.content, self.rt.snapshots.renders), (r.content, renders))   # the same frame: not drawn again
        self.assertEqual(self.c.get("/api/wx/radar/snapshot.png?theme=light", headers={"If-None-Match": r.headers["etag"]}).status_code, 304)
        self.assertEqual(self.c.get("/api/wx/radar/snapshot.png?theme=pink").status_code, 422)
        g = self.c.get("/api/wx/radar/glance").json()
        self.assertTrue(g["fresh"])
        self.assertEqual((g["nearest"]["station"], g["nearest"]["toward"], g["stations"]), ("Conroe", 270, ["Conroe"]))
        self.assertAlmostEqual(g["nearest"]["km"], 20 - 12 * 0.96, delta=3)
        self.assertEqual(g["storms"], 0)                                             # 30 km off: beyond storm_km (10)
        self.rt.radar.cells = [CellsTest.cell(None, 5, valid=int(time.time()))]
        self.assertEqual(self.c.get("/api/wx/radar/glance").json()["storms"], 1)

    def test_no_frame_or_a_stale_one_is_unavailable_not_an_error(self):
        self.c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"})
        r = self.c.get("/api/wx/radar/snapshot.png")
        self.assertEqual((r.status_code, Image.open(io.BytesIO(r.content)).size), (200, SN.SIZE))
        self.rt.radar.field = self.field(blob(20), t=int(time.time()) - SN.STALE - 60)
        stale = self.c.get("/api/wx/radar/snapshot.png")
        self.assertEqual((stale.status_code, stale.content), (200, r.content))          # the same "unavailable" picture
        self.assertEqual((self.c.get("/api/wx/radar/glance").json()["fresh"], self.c.get("/api/wx/radar/glance").json()["nearest"]), (False, None))

    def test_basemap_tiles_are_fetched_once_and_kept(self):
        tile = io.BytesIO()
        Image.new("RGB", (256, 256), (10, 200, 30)).save(tile, format="PNG")
        calls = []

        def get(url, params=None, timeout=None):
            calls.append((url, params))
            return mock.Mock(content=tile.getvalue(), raise_for_status=lambda: None)
        folder = tempfile.mkdtemp()
        maps = SN.Basemap(folder, get=get)
        box = SN.Box.around([(LAT, LON)])
        img = maps.draw(box, "dark", "KEY")
        self.assertEqual((img.size, img.getpixel((10, 10))), (SN.SIZE, (10, 200, 30)))
        self.assertTrue(calls and all("dark_all/8/" in u and p == {"key": "KEY"} for u, p in calls))
        n = len(calls)
        maps.draw(box, "dark", "KEY")
        self.assertEqual(len(calls), n)                                              # from disk the second time
        self.assertIsNone(maps.draw(box, "dark", None))                              # no key: a plain background

        def down(url, params=None, timeout=None):
            raise OSError("offline")
        broken = SN.Basemap(tempfile.mkdtemp(), get=down)
        self.assertIsNone(broken.draw(box, "light", "KEY"))
        self.assertIn("offline", broken.error)
