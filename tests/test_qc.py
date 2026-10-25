"""Station QC's foundation (#105): RTMA read at single points (a few Range requests, not whole fields), the reference
samples kept at each station (RTMA, radar, GLM, NWS alerts, fronts) whether or not the map is open, the past filled in
from the archive, the check framework with its first check (temperature vs RTMA), and /api/wx/qc."""
import os
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from fakes import CFG, FakeFlickr, FakeVision
from test_grib import small_sec3, simple
from test_rtma import FakeS3, N, T, sec3
from coconut_barometer.config import from_dict
from coconut_barometer.store import Store
from coconut_barometer.weather import grib
from coconut_barometer.weather.lambert import Lambert
from coconut_barometer.weather.qc import suites
from coconut_barometer.weather.qc.rtma_points import FIELDS, RtmaPoints, cells
from coconut_barometer.weather.qc.sampler import QUARTER, QcService, distance_bearing, radar_at
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import hash_password

STATION = {"station_id": 7, "name": "Backyard", "latitude": 38.3, "longitude": -105.7, "timezone": "America/Denver",
           "station_meta": {"elevation": 2000.0}, "devices": [{"device_id": 11, "device_type": "ST", "serial_number": "ST-1"}]}


def arr(ts, temp=20.0):
    return [ts, 1.0, 3.0, 5.0, 90.0, 3, 800.0, temp, 50.0, 10000, 2.0, 300.0, 0.0, 0, 0, 0, 2.7, 1]


def analysis(t=T, temp=None, hgt=1900.0, u=3.0, v=0.0):
    """An RTMA file with every field QC reads (in the real file's order) and its .idx. temp: a grid of °C, or 20."""
    temp = np.full((N, N), 20.0) if temp is None else temp
    grids = {"hgt": np.full((N, N), hgt), "pres": np.full((N, N), 80000.0), "t": temp + 273.15, "td": temp + 273.15 - 10,
             "u": np.full((N, N), u), "v": np.full((N, N), v), "gust": np.full((N, N), 7.0), "tcdc": np.full((N, N), 40.0)}
    packs = {"hgt": (0.0, 0, 16), "pres": (50000.0, 0, 16), "t": (25000.0, 2, 16), "td": (25000.0, 2, 16),
             "u": (-2000.0, 2, 13), "v": (-2000.0, 2, 13), "gust": (0.0, 2, 13), "tcdc": (0.0, 0, 8)}
    msgs, idx, at = [], [], 0
    for k, (name, (var, level, _)) in enumerate(FIELDS.items()):
        ref, d, bits = packs[name]
        m = simple(grids[name], sec3(), field=(0, k), ref=ref, d=d, bits=bits, t=t)
        idx.append(f"{k + 1}:{at}:d=2026102419:{var}:{level}:anl:")
        msgs.append(m)
        at += len(m)
    return b"".join(msgs), "\n".join(idx) + "\n"


class GribPointTest(unittest.TestCase):
    def test_single_values_read_from_a_few_bytes_match_the_whole_field(self):
        rng = np.random.default_rng(1)
        for bits in (8, 13, 16, 25):
            values = rng.integers(0, 2 ** bits, size=(7, 9)).astype(float) / 100
            msg = simple(values, small_sec3(9, 7), ref=0.0, d=2, bits=bits)
            h = grib.head(msg[:400])
            self.assertEqual((h["pack"]["bits"], h["grid"]["nx"]), (bits, 9))
            whole = grib.values(grib.message(msg)).ravel()
            for first, last in ((0, 0), (5, 14), (40, 62), (17, 18)):
                a, b = grib.point_bytes(h, first, last)
                got = grib.point_values(h, msg[a:b + 1], a, range(first, last + 1))
                np.testing.assert_allclose(got, whole[first:last + 1], atol=1e-6, err_msg=f"{bits} bits")

    def test_what_isnt_read(self):
        with self.assertRaises(ValueError):
            grib.head(b"GRIB\0\0\0\x02" + b"\0" * 8 + b"\0\0\0\x15\x01")       # stops before section 7
        with self.assertRaises(ValueError):
            grib.head(b"not grib at all")


class RtmaPointsTest(unittest.TestCase):
    def test_a_station_read_from_its_four_cells(self):
        temp = np.tile(np.arange(N, dtype=float), (N, 1)) * 0.1 + 10      # warmer eastward, 0.1 °C a column
        data, idx = analysis(temp=temp)
        s3 = FakeS3({T: (data, idx)})
        got = RtmaPoints(s3).sample(T, [(38.3, -105.7), (60.0, -20.0)])
        p = got["points"][0]
        lam = Lambert(grib.message(simple(temp, sec3()))["grid"])
        i, w = cells(lam, 38.3, -105.7)
        self.assertAlmostEqual(p["t"], float(np.dot(w, temp.ravel()[i])), places=2)
        self.assertEqual((round(p["hgt"]), round(p["pres"]), round(p["td"] - p["t"], 2), round(p["gust"], 2), round(p["tcdc"])),
                         (1900, 800, -10.0, 7.0, 40))
        self.assertAlmostEqual(np.hypot(p["u"], p["v"]), 3.0, places=2)     # turned east and north, same speed
        self.assertIsNone(got["points"][1])                                 # off the grid
        sizes = [int(r.split("-")[1]) - int(r[6:].split("-")[0]) + 1 for _, r in s3.asked if r]
        self.assertLess(max(sizes), 2048)                                   # never a whole field
        self.assertIsNone(RtmaPoints(FakeS3({})).sample(T, [(38.3, -105.7)]))


class FakeRadar:
    def __init__(self, field=None):
        self.lock, self.field = threading.Lock(), field


class FakeFeed:
    def __init__(self, data):
        self.data = data

    def current(self):
        return {"data": self.data}

    def latest(self):
        return self.data


def make_store():
    store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))
    store.wx_save_stations([STATION])
    return store


def service(store, now, files=None, alerts=None, glm=None, fronts=None, field=None, cfg=None):
    cfg = cfg or from_dict({**CFG, "qc": {"backfill_days": 1}})

    def get(url, params=None, headers=None, timeout=None):
        if "api.weather.gov" in url:
            if alerts is None:
                raise ConnectionError("NWS down")
            return mock.Mock(status_code=200, json=lambda: {"features": [{"properties": a} for a in alerts]}, raise_for_status=lambda: None)
        return FakeS3(files or {})(url, headers=headers, timeout=timeout)
    return QcService(cfg, FakeRadar(field), get=get, clock=lambda: now[0], glm_feed=FakeFeed(glm), fronts_feed=FakeFeed(fronts or {}))


class SamplerTest(unittest.TestCase):
    def test_each_source_sampled_at_the_station(self):
        store, now = make_store(), [T + 20 * 60]
        z = np.zeros((200, 200), dtype=np.float32)
        z[100, 100], z[101, 102] = 30.0, 45.0                            # the station's cell, and 2 km away
        field = {"t": T, "north": 38.3 + 100 * 0.01, "west": -105.7 - 100 * 0.01, "di": 0.01, "dj": 0.01, "z": z}
        glm = {"t": np.array([now[0] - 60, now[0] - 60, now[0] - 7200], dtype=np.int64),
               "lat": np.array([3840, 4500, 3840], dtype=np.int16), "lon": np.array([-10570, -10570, -10570], dtype=np.int16), "newest": now[0] - 60}
        fronts = {"valid": T - 3600, "fronts": [{"type": "cold", "points": [[38.0, -106.0], [37.0, -105.0]]},
                                                {"type": "warm", "points": [[60.0, -20.0], [61.0, -21.0]]}], "centers": []}
        alerts = [{"id": "urn:1", "event": "Red Flag Warning", "onset": "2026-10-24T12:00:00-06:00", "ends": "2026-10-24T20:00:00-06:00"}]
        qc = service(store, now, {T: analysis()}, alerts, glm, fronts, field)
        qc.tick(store)
        self.assertEqual({k: v for k, v in qc.errors.items() if v}, {})
        rtma = store.db.execute("SELECT device_id, ts, round(t, 1), round(hgt) FROM qc_rtma").fetchall()
        self.assertEqual([tuple(r) for r in rtma], [(11, T, 20.0, 1900)])
        self.assertEqual(tuple(store.db.execute("SELECT dbz, dbz_near FROM qc_radar").fetchone()), (30.0, 45.0))
        flashes = store.db.execute("SELECT km, bearing FROM qc_glm").fetchall()
        self.assertEqual(len(flashes), 1)                                   # 11 km north; the others too far or too old
        self.assertAlmostEqual(flashes[0][0], 11.1, delta=0.2)
        self.assertLess(min(flashes[0][1], 360 - flashes[0][1]), 1)
        self.assertEqual(store.db.execute("SELECT event FROM qc_alerts").fetchone()[0], "Red Flag Warning")
        import json
        kept = json.loads(store.db.execute("SELECT analysis FROM qc_fronts").fetchone()[0])
        self.assertEqual([f["type"] for f in kept["fronts"]], ["cold"])     # the one near the station
        # Next round: nothing sampled twice, the same radar frame not again.
        qc.tick(store)
        self.assertEqual(store.qc_sampled()["rtma"]["count"], 1)
        self.assertEqual(store.qc_sampled()["radar"]["count"], 1)
        self.assertEqual(store.qc_sampled()["glm"]["count"], 1)

    def test_the_past_filled_in_and_a_source_down_doesnt_stop_the_rest(self):
        store, now = make_store(), [T + 20 * 60]
        hour = int(now[0]) // 3600 * 3600                                 # the archive's hourly analyses, and the newest
        files = {hour - k * 3600: analysis(t=hour - k * 3600) for k in range(1, 30)} | {T: analysis()}
        qc = service(store, now, files, alerts=None)                       # NWS down
        for _ in range(20):
            qc.tick(store)
        self.assertIn("NWS down", qc.errors["alerts"])
        self.assertIsNone(qc.errors["rtma"])
        self.assertTrue(qc.backfill["done"])                               # a day back (qc.backfill_days: 1)
        limit = now[0] - 86400
        back = {hour - k * 3600 for k in range(1, 30) if hour - k * 3600 > limit}
        self.assertEqual(store.qc_rtma_times(0), back | {T})               # every hour of the day, and no further
        self.assertEqual(len(back), 23)

    def test_turned_off_does_nothing(self):
        store, now = make_store(), [T + 20 * 60]
        cfg = from_dict({**CFG, "qc": {"enabled": False}})
        s3 = FakeS3({T: analysis()})
        qc = QcService(cfg, FakeRadar(), get=s3, clock=lambda: now[0], glm_feed=FakeFeed(None), fronts_feed=FakeFeed({}))
        qc.tick(store)
        self.assertEqual((s3.asked, store.qc_sampled()["rtma"]["count"]), ([], 0))

    def test_geometry(self):
        km, bearing = distance_bearing(30.0, -97.0, np.array([31.0, 30.0]), np.array([-97.0, -96.0]))
        self.assertAlmostEqual(km[0], 111.2, delta=0.5)
        self.assertAlmostEqual(bearing[0], 0, delta=0.5)
        self.assertAlmostEqual(bearing[1], 90, delta=0.5)
        field = {"north": 31.0, "west": -98.0, "di": 0.01, "dj": 0.01, "z": np.zeros((10, 10), dtype=np.float32)}
        self.assertIsNone(radar_at(field, 30.0, -97.0))


class TemperatureCheckTest(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.cfg = from_dict({**CFG, "qc": {"min_pairs": 4}})

    def add(self, n, station_temp, rtma_temp, hgt=1900.0):
        for k in range(n):
            ts = T + k * QUARTER
            self.store.wx_save_obs(11, [arr(ts - 60, station_temp), arr(ts, station_temp), arr(ts + 60, station_temp)])
            self.store.qc_save_rtma(11, ts, {"t": rtma_temp, "hgt": hgt})

    def check(self):
        return suites.temperature(self.store, self.store.qc_stations()[0], T - 1, T + 86400, self.cfg)

    def test_the_height_between_station_and_cell_is_allowed_for(self):
        self.add(8, 19.35, 20.0)                     # 100 m above the cell: RTMA's 20 °C is 19.35 °C up here
        r = self.check()
        self.assertEqual(r["status"], "pass")
        self.assertAlmostEqual(r["metrics"]["bias"], 0.0, places=2)
        self.assertEqual((r["metrics"]["n"], r["metrics"]["height_m"]), (8, 100.0))

    def test_warn_fail_and_not_enough(self):
        self.add(8, 21.35, 20.0)
        self.assertEqual(self.check()["status"], "warn")                   # +2 °C
        self.store.db.execute("DELETE FROM wx_obs")
        self.add(8, 23.35, 20.0)
        r = self.check()
        self.assertEqual((r["status"], round(r["metrics"]["bias"], 2)), ("fail", 4.0))
        self.store.db.execute("DELETE FROM qc_rtma")
        self.add(2, 19.35, 20.0)
        self.assertEqual(self.check()["status"], "insufficient")

    def test_run_keeps_each_result_and_says_since_when(self):
        self.add(8, 19.35, 20.0)
        suites.run(self.store, self.cfg, now=T + 86400)
        suites.run(self.store, self.cfg, now=T + 90000)
        latest = self.store.qc_latest(11)["temperature"]
        self.assertEqual((latest["status"], latest["since"], latest["run_at"]), ("pass", T + 86400, T + 90000))


class ApiTest(unittest.TestCase):
    def test_signed_in_only_and_what_it_shows(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        store = Store(path)
        store.wx_save_stations([STATION])
        store.set_password_hash("owner", hash_password("correct horse battery"))
        cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]}})
        c = TestClient(create_app(cfg, FakeFlickr(), provider_factory=lambda: FakeVision("m"), scheduler=False), base_url="https://testserver")
        self.assertEqual(c.get("/api/wx/qc").status_code, 401)
        c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"})
        d = c.get("/api/wx/qc").json()
        self.assertEqual([s["name"] for s in d["stations"]], ["Backyard"])
        self.assertEqual([x["id"] for x in d["suites"]], list(suites.SUITES))
        self.assertEqual(set(d["sampled"]), {"rtma", "radar", "glm", "alerts", "fronts"})
        csrf = c.get("/api/auth/session").json()["csrf"]
        ran = c.post("/api/wx/qc/run", headers={"X-CSRF-Token": csrf}).json()["results"]
        self.assertEqual([(r["suite"], r["status"]) for r in ran], [("temperature", "insufficient")])
        self.assertEqual(c.get("/api/wx/qc").json()["stations"][0]["checks"]["temperature"]["status"], "insufficient")


if __name__ == "__main__":
    unittest.main()
