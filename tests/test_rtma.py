"""The surface analysis (weather/rtma.py): the newest RTMA Rapid Update found by its .idx, only the fields drawn
fetched (a Range each), resampled onto the map's lat/lon levels with the winds turned east and north, cut to the view,
and /api/wx/temperature and /api/wx/wind."""
import base64
import calendar
import os
import struct
import tempfile
import unittest
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from fakes import FakeFlickr, CFG
from test_grib import RTMA_SEC3, simple
from coconut_barometer.config import from_dict
from coconut_barometer.weather import grib, lambert as L, rtma as R
from coconut_barometer.web.app import create_app

T = calendar.timegm((2026, 10, 24, 19, 30, 0, 0, 0, 0))
NOW = T + 20 * 60                       # 19:50Z: the 19:45 file isn't up yet, 19:30's is
N = 60                                  # a 60 × 60 piece of RTMA's grid (150 km), its corner moved to Colorado
CORNER = (38.0, -106.0)


def sec3(nx=N, ny=N, lat=CORNER[0], lon=CORNER[1]) -> bytes:
    s = bytearray(RTMA_SEC3)
    s[30:38] = struct.pack(">II", nx, ny)
    s[38:42] = struct.pack(">I", round(lat * 1e6))
    s[42:46] = struct.pack(">I", round((lon % 360) * 1e6))
    return bytes(s)


def analysis(temp_c=20.0, dew_c=10.0, u=3.0, v=0.0) -> tuple[bytes, str]:
    """An RTMA file of HGT (not wanted), TMP, DPT, UGRD and VGRD, and its .idx, as the real one lays them out."""
    msgs = [simple(np.zeros((N, N)), sec3(), field=(3, 5), ref=0.0, d=0, bits=8, t=T),
            simple(np.full((N, N), temp_c + 273.15), sec3(), field=(0, 0), ref=25000.0, d=2, bits=16, t=T),
            simple(np.full((N, N), dew_c + 273.15), sec3(), field=(0, 6), ref=25000.0, d=2, bits=16, t=T),
            simple(np.full((N, N), u), sec3(), field=(2, 2), ref=-2000.0, d=2, bits=13, t=T),
            simple(np.full((N, N), v), sec3(), field=(2, 3), ref=-2000.0, d=2, bits=13, t=T)]
    names = ["HGT:surface", "TMP:2 m above ground", "DPT:2 m above ground",
             "UGRD:10 m above ground", "VGRD:10 m above ground"]
    idx, at = [], 0
    for k, (m, name) in enumerate(zip(msgs, names)):
        idx.append(f"{k + 1}:{at}:d=2026102419:{name}:anl:")
        at += len(m)
    return b"".join(msgs), "\n".join(idx) + "\n"


class FakeS3:
    """The bucket: the .idx of each analysis there, and the file's bytes by Range."""

    def __init__(self, files: dict):
        self.files, self.asked = files, []

    def __call__(self, url, headers=None, timeout=None):
        self.asked.append((url, (headers or {}).get("Range")))
        for t, (data, idx) in self.files.items():
            if url == R.url(t) + ".idx":
                return mock.Mock(status_code=200, text=idx, raise_for_status=lambda: None)
            if url == R.url(t):
                a, b = headers["Range"][6:].split("-")
                return mock.Mock(status_code=206, content=data[int(a):int(b) + 1 if b else None], raise_for_status=lambda: None)
        return mock.Mock(status_code=404)


def unpack(view, name):
    a = np.frombuffer(base64.b64decode(view["fields"][name]), "<i2").reshape(view["ny"], view["nx"]).astype(float)
    a[a == view["missing"]] = np.nan
    return a * view["scale"]


class RtmaTest(unittest.TestCase):
    def test_the_fields_in_a_real_idx(self):
        idx = ("1:0:d=2026102419:HGT:surface:anl:\n2:7490118:d=2026102419:PRES:surface:anl:\n"
               "3:14980236:d=2026102419:TMP:2 m above ground:anl:\n4:21065993:d=2026102419:DPT:2 m above ground:anl:\n"
               "5:27151750:d=2026102419:UGRD:10 m above ground:anl:\n6:33237507:d=2026102419:VGRD:10 m above ground:anl:\n"
               "7:38855143:d=2026102419:SPFH:2 m above ground:anl:\n")
        self.assertEqual(R.ranges(idx), {"t": (14980236, 21065992), "d": (21065993, 27151749),
                                         "u": (27151750, 33237506), "v": (33237507, 38855142)})
        self.assertEqual(R.ranges(idx.rsplit("7:", 1)[0])["v"], (33237507, None))       # the last: to the end
        with self.assertRaisesRegex(ValueError, "t"):
            R.ranges(idx.replace("TMP", "TMAX"))
        self.assertEqual(R.url(T), "https://noaa-rtma-pds.s3.amazonaws.com/rtma2p5_ru.20261024/rtma2p5_ru.t1930z.2dvaranl_ndfd.grb2")

    def test_the_newest_fetched_and_only_what_is_drawn(self):
        s3 = FakeS3({T: analysis()})
        f = R.Rtma(get=s3, clock=lambda: NOW, background=False)
        state = f.current()
        self.assertEqual((state["error"], state["data"]["valid"]), (None, T))
        self.assertEqual([u for u, _ in s3.asked[:2]], [R.url(T + 900) + ".idx", R.url(T) + ".idx"])   # 19:45: not yet
        self.assertEqual(len([r for _, r in s3.asked if r]), 4)                  # TMP, DPT, UGRD, VGRD; not HGT
        s3.asked.clear()
        f.checked_at -= R.TTL
        f.current()                                                              # nothing newer: nothing downloaded
        self.assertEqual(s3.asked, [(R.url(T + 900) + ".idx", None)])

    def test_resampled_cut_to_the_view_with_the_wind_turned(self):
        f = R.Rtma(get=FakeS3({T: analysis(temp_c=20.0, u=3.0, v=0.0)}), clock=lambda: NOW, background=False)
        lam = L.Lambert(grib.message(analysis()[0][len(simple(np.zeros((N, N)), sec3(), field=(3, 5), ref=0.0, d=0, bits=8, t=T)):])["grid"])
        center = (38.6, -105.3)                                                  # inside the 150 km piece
        v = f.view(("t", "u", "v"), (-105.5, 38.4, -105.1, 38.8), zoom=9)
        self.assertEqual((v["level"], v["valid"], v["scale"]), ("fine", T, 0.1))
        self.assertEqual((v["lat0"], v["lon0"], v["ny"], v["nx"]), (38.4, -105.5, 9, 9))
        j, i = round((center[0] - v["lat0"]) / v["dlat"]), round((center[1] - v["lon0"]) / v["dlon"])
        self.assertAlmostEqual(unpack(v, "t")[j, i], 20.0, places=1)
        ue, ve = lam.earth_relative(np.float32(3.0), np.float32(0.0), center[1])
        self.assertAlmostEqual(unpack(v, "u")[j, i], float(ue), delta=0.06)
        self.assertAlmostEqual(unpack(v, "v")[j, i], float(ve), delta=0.06)     # west of LoV: turned toward the north
        self.assertGreater(unpack(v, "v")[j, i], 0.1)
        moisture = f.view(("d", "rh"), (-105.5, 38.4, -105.1, 38.8), zoom=9)
        self.assertAlmostEqual(unpack(moisture, "d")[j, i], 10.0, places=1)
        self.assertAlmostEqual(unpack(moisture, "rh")[j, i], 52.5, delta=0.2)
        us = f.view(("t",), None, zoom=4)
        self.assertEqual((us["level"], us["nx"], us["ny"], list(us["fields"])), ("coarse", 281, 133, ["t"]))
        self.assertTrue(np.isnan(unpack(us, "t")[0, 0]))                         # off the grid: no value
        self.assertEqual(f.view(("t",), (10, 10, 11, 11), zoom=9)["fields"], {})  # outside the US: nothing

    def test_levels_by_zoom(self):
        self.assertEqual([R.level_for(z) for z in (None, 3, 5.9, 6, 7.5, 8, 12)], ["coarse", "coarse", "coarse", "mid", "mid", "fine", "fine"])

    def test_nothing_up_yet_and_s3_down(self):
        f = R.Rtma(get=FakeS3({}), clock=lambda: NOW, background=False)
        self.assertEqual((f.current()["data"], f.current()["error"]), (None, None))
        self.assertEqual(f.view(("t",))["fields"], {})
        def down(*a, **k):
            raise ConnectionError("no route to host")
        f2 = R.Rtma(get=down, clock=lambda: NOW, background=False)
        self.assertIn("no route", f2.view(("t",))["error"])

    def test_the_endpoints(self):
        cfg = from_dict({**CFG, "database": os.path.join(tempfile.mkdtemp(), "db.duckdb"), "web": {"allowed_hosts": ["testserver"]}})
        c = TestClient(create_app(cfg, FakeFlickr(), scheduler=False), base_url="https://testserver")
        with mock.patch.object(R, "rtma", R.Rtma(get=FakeS3({T: analysis()}), clock=lambda: NOW, background=False)):
            t = c.get("/api/wx/temperature?bbox=-106,38,-105,39&zoom=7").json()
            w = c.get("/api/wx/wind?zoom=4").json()
            d = c.get("/api/wx/dewpoint?zoom=4").json()
            h = c.get("/api/wx/humidity?zoom=4").json()
        self.assertEqual((t["level"], list(t["fields"]), t["valid"], t["error"]), ("mid", ["t"], T, None))
        self.assertEqual((w["level"], sorted(w["fields"]), w["units"]), ("coarse", ["u", "v"], t["units"]))
        self.assertEqual((d["level"], sorted(d["fields"])), ("coarse", ["d"]))
        self.assertEqual((h["level"], sorted(h["fields"])), ("coarse", ["rh"]))


if __name__ == "__main__":
    unittest.main()
