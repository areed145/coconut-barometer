"""Surface fronts (weather/fronts.py): WPC's coded surface bulletin read into fronts and highs and lows, fetched only
when it changed, the last analysis kept while WPC is down, and /api/wx/fronts."""
import calendar
import os
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from fakes import FakeFlickr, CFG
from coconut_barometer.config import from_dict
from coconut_barometer.weather import fronts as F
from coconut_barometer.web.app import create_app

# The start of a real high-resolution bulletin (24 October 2026, 09Z).
BULLETIN = """\
CODED SURFACE FRONTAL POSITIONS
NWS WEATHER PREDICTION CENTER COLLEGE PARK MD
622 AM EDT SAT OCT 24 2026
VALID 102409Z
HIGHS 1033 6431100 1037 6361329 1037 6191445 1030 4501181 1034 4691118 1028
4221314 1036 4470727 1036 4780729 1012 6810448 1031 5641216 1014 3561116
LOWS 1006 5101350 993 5431017 984 4380555 997 6490571 1002 6830757 1001 7170648
996 6070419 1009 3501142 1010 3531085 1009 4111015
COLD 5101351 4941351 4821373 4751415
STNRY 4751414 4711436 4701447
WARM 5091351 5111326 5031298 4861274
STNRY 5820629 5820669 5770717 5670763 5520796 5430834 5390895 5410973 5431017
COLD 6050557 5890573 5820602 5820630
OCFNT 6480573 6370558 6160553 6050557
TROF 5471022 5641053 5811090 6081122
TROF 6471530 6501504 6491463 6371431 6231408
TROF 6841733 6941699 7091627 7201519 7091421 6721327
TROF 6820760 6480815 6140867
TROF 7180648 7450653 7820719
OCFNT 6491782 6591757 6671714 6631657 6431616 6031601 5751605

$$
"""
NOW = calendar.timegm((2026, 10, 24, 10, 22, 0, 0, 0, 0))


class ParseTest(unittest.TestCase):
    def test_a_real_bulletin(self):
        d = F.parse(BULLETIN, NOW)
        self.assertEqual(d["valid"], calendar.timegm((2026, 10, 24, 9, 0, 0, 0, 0, 0)))
        self.assertEqual(d["fronts"][0], {"type": "cold", "points": [[51.0, -135.1], [49.4, -135.1], [48.2, -137.3], [47.5, -141.5]]})
        self.assertEqual([f["type"] for f in d["fronts"][:6]], ["cold", "stationary", "warm", "stationary", "cold", "occluded"])
        highs = [c for c in d["centers"] if c["kind"] == "H"]
        lows = [c for c in d["centers"] if c["kind"] == "L"]
        self.assertEqual((len(highs), len(lows)), (11, 10))                 # two pressures wrap onto the next line
        self.assertEqual(highs[5], {"kind": "H", "mb": 1028, "lat": 42.2, "lon": -131.4})
        self.assertEqual(lows[1], {"kind": "L", "mb": 993, "lat": 54.3, "lon": -101.7})

    def test_places_and_times(self):
        self.assertEqual(F.place("5101350"), (51.0, -135.0))
        self.assertEqual(F.place("51135"), (51.0, -135.0))                  # the older, whole-degree bulletin
        jan = calendar.timegm((2027, 1, 1, 2, 0, 0, 0, 0, 0))
        self.assertEqual(F.valid_time("123121Z", jan), calendar.timegm((2026, 12, 31, 21, 0, 0, 0, 0, 0)))   # last year's
        with self.assertRaises(ValueError):
            F.parse("NOT A BULLETIN", NOW)


class FetchTest(unittest.TestCase):
    def test_fetched_only_when_changed_and_kept_while_wpc_is_down(self):
        now, calls, answers = [NOW], [], []
        def get(url, headers=None, timeout=None):
            calls.append(headers)
            return answers.pop(0)
        ok = mock.Mock(status_code=200, text=BULLETIN, raise_for_status=lambda: None, headers={"Last-Modified": "Sat, 24 Oct 2026 10:22:30 GMT"})
        fr = F.Fronts(get=get, clock=lambda: now[0], ttl=900)
        answers.append(ok)
        first = fr.latest()
        self.assertEqual((len(first["fronts"]) > 5, first["error"]), (True, None))
        self.assertEqual(fr.latest()["valid"], first["valid"])
        self.assertEqual(len(calls), 1)                                     # within TTL: not asked again
        now[0] += 900
        answers.append(mock.Mock(status_code=304))
        self.assertEqual(fr.latest()["valid"], first["valid"])
        self.assertEqual(calls[1], {"If-Modified-Since": "Sat, 24 Oct 2026 10:22:30 GMT"})
        now[0] += 900
        def down():
            raise ConnectionError("no route to host")
        answers.append(mock.Mock(status_code=503, raise_for_status=down))
        kept = fr.latest()
        self.assertEqual(kept["fronts"], first["fronts"])                   # the last analysis, with the error
        self.assertIn("no route", kept["error"])

    def test_the_endpoint(self):
        cfg = from_dict({**CFG, "database": os.path.join(tempfile.mkdtemp(), "db.duckdb"), "web": {"allowed_hosts": ["testserver"]}})
        c = TestClient(create_app(cfg, FakeFlickr(), scheduler=False), base_url="https://testserver")
        ok = mock.Mock(status_code=200, text=BULLETIN, raise_for_status=lambda: None, headers={})
        with mock.patch.object(F, "fronts", F.Fronts(get=lambda *a, **k: ok, clock=lambda: NOW)):
            d = c.get("/api/wx/fronts").json()
        self.assertEqual((d["valid"], d["fronts"][0]["type"], d["error"]), (calendar.timegm((2026, 10, 24, 9, 0, 0, 0, 0, 0)), "cold", None))


if __name__ == "__main__":
    unittest.main()
