"""Rain totals (weather/qpe.py, #177, #179): MRMS's QPE averaged to ~2 km, specks dropped, and traced into stacked
shapes per rain level as the radar's are; the `latest` file fetched again only when it has changed; /api/wx/rain."""
import calendar
import gzip
import json
import os
import tempfile
import unittest
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from fakes import FakeFlickr, CFG
from test_radar import grib
from coconut_barometer.config import from_dict
from coconut_barometer.weather import qpe as Q, radar as R
from coconut_barometer.web.app import create_app

T = calendar.timegm((2026, 10, 28, 11, 0, 0, 0, 0, 0))
LA1, LO1 = 30.995, -95.995                   # the first point's centre, as MRMS's grid starts half a point in


def total() -> np.ndarray:
    """100 × 100 points of 0.01° (mm, -3: no radar), north at the top: 30 mm over a 30 × 30 patch in the middle, one
    point of 50 mm on its own (a speck), and the east tenth unseen."""
    mm = np.zeros((100, 100))
    mm[35:65, 35:65] = 30.0
    mm[10, 10] = 50.0
    mm[:, 90:] = -3
    return mm


class Resp:
    def __init__(self, status: int, content: bytes = b"", headers: dict | None = None):
        self.status_code, self.content, self.headers = status, content, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeMrms:
    """The MRMS server: each product's latest file, modified at `modified`, and what was asked."""

    def __init__(self, modified="Wed, 28 Oct 2026 11:15:32 GMT"):
        self.modified, self.asked = modified, []

    def __call__(self, url, headers=None, timeout=None):
        self.asked.append((url, dict(headers or {})))
        if (headers or {}).get("If-Modified-Since") == self.modified:
            return Resp(304)
        return Resp(200, gzip.compress(grib(total(), la1=LA1, lo1=LO1, t=T)), {"Last-Modified": self.modified})


def levels(shapes_text: str) -> list[float]:
    return [b["mm"] for b in json.loads(shapes_text)["bands"]]


class QpeTest(unittest.TestCase):
    def test_averaged_to_2_km_with_the_unseen_as_dry(self):
        msg = R.decode(grib(total(), la1=LA1, lo1=LO1, t=T))
        f = Q.field(msg, R.grid(msg))
        self.assertEqual((f["mm"].shape, f["di"], f["dj"]), ((50, 50), 0.02, 0.02))
        self.assertAlmostEqual(f["north"], 30.99, places=6)                  # the first block's centre
        self.assertAlmostEqual(f["west"], -95.99, places=6)
        self.assertEqual((f["mm"][20, 20], f["mm"][5, 5], f["mm"][20, 47]), (30.0, 12.5, 0.0))   # patch, speck, unseen

    def test_stacked_shapes_per_level_without_the_speck(self):
        msg = R.decode(grib(total(), la1=LA1, lo1=LO1, t=T))
        out = Q.shapes(msg, R.grid(msg))
        self.assertEqual(list(out), ["fine", "coarse"])
        fine = [mm for mm, _ in out["fine"]]
        self.assertEqual(fine, Q.BANDS[:len(fine)])                        # each level up from the lowest
        self.assertEqual(fine[-1], 25.4)                                   # 1 in: the patch's 30 mm, smoothed a little
        lowest = out["fine"][0][1]
        self.assertEqual(len(lowest), 1)                                   # the patch only: the speck is gone
        w, s, e, n = (x / Q.LEVELS["fine"][2] for x in lowest[0][:4])
        # Centred on the patch (-95.5, 30.5; 0.3° across), smoothing spreading its faintest edge ~0.1° beyond it.
        self.assertAlmostEqual((w + e) / 2, -95.5, delta=0.02)
        self.assertAlmostEqual((s + n) / 2, 30.5, delta=0.02)
        self.assertTrue(0.3 < e - w < 0.6 and 0.3 < n - s < 0.6, (w, s, e, n))
        self.assertTrue(out["coarse"])

    def test_fetched_again_only_when_changed_and_cut_to_the_view(self):
        mrms, now = FakeMrms(), [T + 1000]
        totals = Q.Totals(get=mrms, clock=lambda: now[0], background=False)
        rest, shapes = totals.view(24, (-96, 30, -95, 31), zoom=9)
        self.assertEqual((rest["hours"], rest["valid"], rest["error"]), (24, T, None))
        self.assertEqual(mrms.asked[0][0], "https://mrms.ncep.noaa.gov/2D/MultiSensor_QPE_24H_Pass1/MRMS_MultiSensor_QPE_24H_Pass1.latest.grib2.gz")
        self.assertEqual((json.loads(shapes)["level"], levels(shapes)[-1]), ("fine", 25.4))
        self.assertEqual(levels(totals.view(24, (-90, 40, -89, 41), zoom=9)[1]), [])     # nothing in view
        self.assertEqual(json.loads(totals.view(24, None, zoom=7)[1])["level"], "coarse")   # fine from FINE_ZOOM
        now[0] += Q.TTL
        totals.view(24)
        self.assertEqual(mrms.asked[-1][1], {"If-Modified-Since": mrms.modified})   # not changed: kept
        self.assertEqual(totals.view(24)[0]["valid"], T)
        totals.view(3)
        self.assertIn("MultiSensor_QPE_03H_Pass1", mrms.asked[-1][0])     # each period its own feed
        self.assertIs(totals.feed(3), totals.feed(3))

    def test_unreachable_keeps_no_data_and_says_why(self):
        def down(*a, **k):
            raise ConnectionError("no route to host")
        rest, shapes = Q.Totals(get=down, background=False).view(6)
        self.assertEqual((rest["valid"], rest["hours"], json.loads(shapes)["bands"]), (None, 6, []))
        self.assertIn("no route", rest["error"])

    def test_the_endpoint(self):
        cfg = from_dict({**CFG, "database": os.path.join(tempfile.mkdtemp(), "db.duckdb"), "web": {"allowed_hosts": ["testserver"]}})
        c = TestClient(create_app(cfg, FakeFlickr(), scheduler=False), base_url="https://testserver")
        with mock.patch.object(Q, "qpe", Q.Totals(get=FakeMrms(), clock=lambda: T + 1000, background=False)):
            r = c.get("/api/wx/rain?hours=6&zoom=4").json()
            self.assertEqual(c.get("/api/wx/rain?hours=5").status_code, 422)
            self.assertEqual(c.get("/api/wx/rain").json()["hours"], 24)
        self.assertEqual((r["hours"], r["valid"], r["units"], r["shapes"]["level"]), (6, T, "metric", "coarse"))
        self.assertEqual(r["shapes"]["bands"][0]["mm"], Q.BANDS[0])


if __name__ == "__main__":
    unittest.main()
