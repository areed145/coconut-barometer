"""Rain totals (weather/qpe.py, #177): MRMS's QPE averaged onto the map's levels, what no radar sees kept apart from no
rain, the `latest` file fetched again only when it has changed, and /api/wx/rain."""
import base64
import calendar
import gzip
import os
import tempfile
import unittest
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from fakes import FakeFlickr, CFG
from test_radar import grib
from coconut_barometer.config import from_dict
from coconut_barometer.weather import qpe as Q
from coconut_barometer.weather.rtma import MISSING
from coconut_barometer.web.app import create_app

T = calendar.timegm((2026, 10, 28, 11, 0, 0, 0, 0, 0))
LA1, LO1 = 40.995, -100.995                  # the first point's centre, as MRMS's grid starts half a point in


def field() -> np.ndarray:
    """20 × 20 points of 0.01° (mm, -3: no radar), north at the top: the NW 10 × 10 block 4 mm, half of it unseen;
    the NE block unseen; the south half 0 but for one point of 50 mm in the SW block."""
    mm = np.zeros((20, 20))
    mm[:10, :10] = 4.0
    mm[:5, :10] = -3
    mm[:10, 10:] = -3
    mm[15, 2] = 50.0
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
        return Resp(200, gzip.compress(grib(field(), la1=LA1, lo1=LO1, t=T)), {"Last-Modified": self.modified})


def unpack(view: dict) -> np.ndarray:
    raw = np.frombuffer(base64.b64decode(view["fields"]["p"]), "<i2").reshape(view["ny"], view["nx"])
    return np.where(raw == view["missing"], np.nan, raw * view["scale"])


class QpeTest(unittest.TestCase):
    def test_each_level_is_the_mean_of_what_is_seen(self):
        from coconut_barometer.weather import radar
        msg = radar.decode(grib(field(), la1=LA1, lo1=LO1, t=T))
        levels = Q.average(msg, radar.grid(msg))
        coarse = levels["coarse"]
        self.assertEqual(coarse["box"], {"lat0": 40.85, "lon0": -100.95, "dlat": 0.1, "dlon": 0.1, "ny": 2, "nx": 2})
        p = np.where(coarse["p"] == MISSING, np.nan, coarse["p"] / 10)          # rows south first
        self.assertEqual(p[1, 0], 4.0)                         # NW: the seen half's mean, not dragged down by the rest
        self.assertTrue(np.isnan(p[1, 1]))                      # NE: no radar, not "no rain"
        self.assertEqual((p[0, 0], p[0, 1]), (0.5, 0.0))        # SW: one wet point in a hundred; SE: dry
        fine = levels["fine"]
        self.assertEqual((fine["box"]["lat0"], fine["box"]["lon0"], fine["box"]["ny"]), (40.81, -100.99, 10))
        self.assertEqual(fine["p"][2, 1], 125)                  # the 50 mm point, in a 2 × 2 block: 12.5 mm
        self.assertEqual(list(levels), ["coarse", "mid", "fine"])

    def test_fetched_again_only_when_changed_and_cut_to_the_view(self):
        mrms, now = FakeMrms(), [T + 1000]
        totals = Q.Totals(get=mrms, clock=lambda: now[0], background=False)
        v = totals.view(24, (-101, 40.8, -100.9, 41), zoom=9)
        self.assertEqual((v["hours"], v["valid"], v["level"], v["error"]), (24, T, "fine", None))
        self.assertEqual(mrms.asked[0][0], "https://mrms.ncep.noaa.gov/2D/MultiSensor_QPE_24H_Pass1/MRMS_MultiSensor_QPE_24H_Pass1.latest.grib2.gz")
        self.assertEqual((v["lat0"], v["lon0"], v["nx"]), (40.81, -100.99, 6))   # to just past the box
        self.assertEqual(unpack(v)[2, 1], 12.5)
        now[0] += Q.TTL
        totals.view(24)
        self.assertEqual(mrms.asked[-1][1], {"If-Modified-Since": mrms.modified})   # not changed: kept
        self.assertEqual(totals.view(24, zoom=4)["valid"], T)
        totals.view(3)
        self.assertIn("MultiSensor_QPE_03H_Pass1", mrms.asked[-1][0])     # each period its own feed
        self.assertIs(totals.feed(3), totals.feed(3))

    def test_unreachable_keeps_no_data_and_says_why(self):
        def down(*a, **k):
            raise ConnectionError("no route to host")
        v = Q.Totals(get=down, background=False).view(6)
        self.assertEqual((v["fields"], v["valid"], v["hours"]), ({}, None, 6))
        self.assertIn("no route", v["error"])

    def test_the_endpoint(self):
        cfg = from_dict({**CFG, "database": os.path.join(tempfile.mkdtemp(), "db.duckdb"), "web": {"allowed_hosts": ["testserver"]}})
        c = TestClient(create_app(cfg, FakeFlickr(), scheduler=False), base_url="https://testserver")
        with mock.patch.object(Q, "qpe", Q.Totals(get=FakeMrms(), clock=lambda: T + 1000, background=False)):
            r = c.get("/api/wx/rain?hours=6&zoom=4").json()
            self.assertEqual(c.get("/api/wx/rain?hours=5").status_code, 422)
            self.assertEqual(c.get("/api/wx/rain").json()["hours"], 24)
        self.assertEqual((r["hours"], r["level"], list(r["fields"]), r["valid"], r["units"]), (6, "coarse", ["p"], T, "metric"))


if __name__ == "__main__":
    unittest.main()
