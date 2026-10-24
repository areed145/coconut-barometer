"""Radar near the stations (weather/radar.py): an MRMS frame decoded, cropped, cleaned and traced into shapes; storm
cells read, merged and carried along their course; and the storm and rain alerts built on them."""
import calendar
import gzip
import io
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
from coconut_barometer import push
from coconut_barometer import settings as S
from coconut_barometer.alerts import Alerts
from coconut_barometer.config import from_dict
from coconut_barometer.store import Store
from coconut_barometer.weather import radar as R
from coconut_barometer.web.app import create_app

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
        bands = R.bands(f, z)
        self.assertEqual([b["properties"]["dbz"] for b in bands][0], 15)
        lons = [p[0] for b in bands for poly in b["geometry"]["coordinates"] for ring in poly for p in ring]
        self.assertTrue(all(LON - 0.35 < x < LON - 0.05 for x in lons))   # the shapes are where the rain is

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


class CellsTest(unittest.TestCase):
    def cell(self, west_km=30, toward=90, kmh=60, valid=T, dbz=55, **kw):
        return {"id": "HGX-A1", "lat": LAT, "lon": LON - west_km / (R.KM_PER_DEG * math.cos(math.radians(LAT))),
                "valid": valid, "toward": toward, "kmh": kmh, "dbz": dbz, "posh": 0, "poh": 0, "tvs": False, **kw}

    def test_iem_motion_is_where_it_comes_from(self):
        cells = R.cells_from({"features": [{"properties": {"nexrad": "HGX", "storm_id": "A1", "drct": 270, "sknt": 20,
                                                           "max_dbz": 55, "valid": "2026-10-24T00:20:39Z"},
                                            "geometry": {"type": "Point", "coordinates": [LON, LAT]}}]})
        self.assertEqual((cells[0]["toward"], round(cells[0]["kmh"], 1), cells[0]["valid"]), (90, 37.0, T))

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

    def test_a_storm_on_its_way(self):
        self.radar.cells = [CellsTest.cell(None, posh=40)]
        a = self.by_kind()["storm"]
        self.assertEqual(a["title"], "Storm ~20 min from Conroe")
        self.assertEqual(a["body"], "55 dBZ, moving E at 60 km/h; hail likely (40% severe).")
        self.radar.cells = [CellsTest.cell(None, dbz=40)]                # under alerts.storm_dbz
        self.assertNotIn("storm", self.by_kind())

    def test_rain_closing_in(self):
        for i, km in enumerate((30, 25, 20)):
            self.radar.rain.setdefault(11, R.deque(maxlen=20)).append((T - 480 + i * 240, km))
        a = self.by_kind()["rain"]
        self.assertEqual(a["title"], "Rain ~14 min from Conroe")          # (20 − 3) km at 75 km/h
        self.assertEqual(a["body"], "20 km away, closing at 75 km/h.")
        self.cfg["alerts"]["rain_minutes"] = 10
        self.assertNotIn("rain", self.by_kind())


class RadarApiTest(unittest.TestCase):
    def test_the_map_gets_shapes_colors_and_cells(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]}})
        app = create_app(cfg, FakeFlickr(), scheduler=False)
        rt = app.state.runtime
        rt.radar.frame = {"t": T, "box": (27, 33, -98.5, -92.5), "bands": [{"type": "Feature", "properties": {"dbz": 15},
                                                                              "geometry": {"type": "MultiPolygon", "coordinates": []}}]}
        rt.radar.cells = [CellsTest.cell(None)]
        d = TestClient(app, base_url="https://testserver").get("/api/wx/radar").json()
        self.assertEqual((d["t"], len(d["bands"]["features"]), d["colors"][0]), (T, 1, {"dbz": 15, "color": "#8fd3ff"}))
        self.assertEqual(len(d["cells"][0]["path"]), 3)
        self.assertGreater(d["cells"][0]["path"][2][1], d["cells"][0]["path"][0][1])   # heading east


if __name__ == "__main__":
    unittest.main()
