"""Watches and warnings (weather/hazards.py): NOAA's WWA features merged per alert, expired ones dropped, the NWS
hazard map's colors and order, advisories and marine alerts marked minor, two levels of shapes and only what's in view,
fetched only when changed (ETag) and kept while the service is down, and /api/wx/hazards with its popup text."""
import calendar
import json
import os
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from fakes import FakeFlickr, CFG
from coconut_barometer.config import from_dict
from coconut_barometer.weather import hazards as H
from coconut_barometer.web.app import create_app

NOW = calendar.timegm((2026, 10, 24, 20, 0, 0, 0, 0, 0))


def square(lon, lat, size):
    return [[lon, lat], [lon + size, lat], [lon + size, lat + size], [lon, lat + size], [lon, lat]]


def feature(event, cap_id, rings, sig="W", phenom="FL", expiration="2026-10-25T02:00:00-06:00", wfo="KPUB", multi=False):
    geometry = {"type": "MultiPolygon", "coordinates": [[r] for r in rings]} if multi else {"type": "Polygon", "coordinates": rings}
    return {"type": "Feature", "geometry": geometry,
            "properties": {"prod_type": event, "sig": sig, "phenom": phenom, "onset": "2026-10-24T13:55:00-06:00",
                           "ends": "2026-10-26T06:00:00-06:00", "expiration": expiration, "wfo": wfo, "cap_id": cap_id}}


# As the service sends them: one feature per alert per zone, a Special Weather Statement with blank VTEC codes.
FEATURES = [
    feature("Flood Watch", "urn:watch", [square(-106, 37, 1)], sig="A", phenom="FA"),
    feature("Flood Watch", "urn:watch", [square(-105, 37, 1), square(-104, 37, 1)], sig="A", phenom="FA", multi=True),
    feature("Tornado Warning", "urn:tor", [square(-105.5, 37.2, 0.3)], phenom="TO", wfo="KPUB"),
    feature("Special Weather Statement", "urn:sps", [square(-100, 40, 0.5)], sig=" ", phenom=" ", wfo=" "),
    feature("Small Craft Advisory", "urn:sca", [square(-80, 30, 0.5)], sig="Y", phenom="SC"),
    feature("Gale Warning", "urn:gale", [square(-79, 30, 0.5)], phenom="GL"),
    feature("Flood Warning", "urn:old", [square(-90, 35, 1)], expiration="2026-10-24T12:00:00-06:00"),   # expired 18Z
    feature("Volcanic Haze Notice", "urn:odd", [square(-155, 19, 0.5)], sig="Y", phenom="VH"),
    feature("Flash Flood Warning", "urn:tiny", [square(-95, 30, 0.012)], phenom="FF"),
]


def answer(features, status=200, etag='"e1"'):
    return mock.Mock(status_code=status, raise_for_status=lambda: None, json=lambda: {"features": features}, headers={"ETag": etag})


class AlertsTest(unittest.TestCase):
    def test_merged_per_alert_unexpired_and_most_important_first(self):
        alerts = H.alerts_from(FEATURES, NOW)
        self.assertEqual([a["id"] for a in alerts], ["urn:tor", "urn:tiny", "urn:gale", "urn:sca", "urn:watch", "urn:sps", "urn:odd"])
        watch = next(a for a in alerts if a["id"] == "urn:watch")
        self.assertEqual(len(watch["polygons"]), 3)                          # three zones, one alert
        self.assertEqual((watch["color"], watch["sig"], watch["minor"]), ("#2E8B57", "A", False))
        self.assertEqual(watch["expires"], calendar.timegm((2026, 10, 25, 8, 0, 0, 0, 0, 0)))
        tor = alerts[0]
        self.assertEqual((tor["event"], tor["priority"], tor["color"]), ("Tornado Warning", 2, "#FF0000"))

    def test_colors_minor_and_the_fallback(self):
        alerts = {a["id"]: a for a in H.alerts_from(FEATURES, NOW)}
        self.assertEqual((alerts["urn:sps"]["sig"], alerts["urn:sps"]["wfo"], alerts["urn:sps"]["color"]), ("", "", "#FFE4B5"))
        self.assertEqual({k for k, a in alerts.items() if a["minor"]}, {"urn:sps", "urn:sca", "urn:gale", "urn:odd"})
        self.assertEqual(alerts["urn:odd"]["color"], H.BY_SIG["Y"])                # not in the NWS table: by significance
        self.assertGreater(alerts["urn:odd"]["priority"], len(H.HAZARDS))
        self.assertEqual(H.look("Flash Flood Statement", "")[2], False)           # a statement, but among the most urgent
        self.assertEqual(H.look("Hurricane Watch", "A"), (54, "#FF00FF", False))

    def test_two_levels_and_only_whats_in_view(self):
        h = H.Hazards(get=None, clock=lambda: NOW)
        h.data, h.checked_at = {"alerts": H.alerts_from(FEATURES, NOW)}, NOW
        us = json.loads(h.view()[0])
        self.assertEqual((us["level"], us["q"], len(us["alerts"])), ("coarse", 20, 7))
        tiny = next(a for a in us["alerts"] if a["id"] == "urn:tiny")
        self.assertEqual(tiny["polys"][0][:4], [-9500, 3000, -9499, 3001])      # too small for 0.05°: its fine shape
        near = json.loads(h.view((-107, 36, -103, 39), 7)[0])
        self.assertEqual((near["level"], near["q"]), ("fine", 100))
        self.assertEqual([a["id"] for a in near["alerts"]], ["urn:tor", "urn:watch"])
        self.assertEqual(near["alerts"][0]["polys"][0][:4], [-10550, 3720, -10520, 3750])
        later = json.loads(h.view(now=calendar.timegm((2026, 10, 25, 9, 0, 0, 0, 0, 0)))[0])
        self.assertEqual(later["alerts"], [])                                    # expired since: dropped though kept


class FetchTest(unittest.TestCase):
    def test_fetched_only_when_changed_and_kept_while_the_service_is_down(self):
        now, calls, answers = [NOW], [], []
        def get(url, params=None, headers=None, timeout=None):
            calls.append((params["resultOffset"], headers))
            return answers.pop(0)
        h = H.Hazards(get=get, clock=lambda: now[0], background=False)
        answers.append(answer(FEATURES))
        first = h.current()
        self.assertEqual((len(first["data"]["alerts"]), first["error"]), (7, None))
        self.assertEqual(calls[0][1]["User-Agent"], H.USER_AGENT)
        now[0] += H.TTL
        answers.append(mock.Mock(status_code=304))
        self.assertEqual(len(h.current()["data"]["alerts"]), 7)
        self.assertEqual(calls[1][1]["If-None-Match"], '"e1"')
        now[0] += H.TTL
        def down():
            raise ConnectionError("no route to host")
        answers.append(mock.Mock(status_code=503, raise_for_status=down))
        kept = h.current()
        self.assertEqual((len(kept["data"]["alerts"]), "no route" in kept["error"]), (7, True))

    def test_more_than_a_page(self):
        pages = [answer(FEATURES[:2]), answer(FEATURES[2:4]), answer(FEATURES[4:5])]
        offsets = []
        def get(url, params=None, headers=None, timeout=None):
            offsets.append((params["resultOffset"], "If-None-Match" in headers))
            return pages.pop(0)
        h = H.Hazards(get=get, clock=lambda: NOW, background=False)
        h._validators = {"ETag": '"old"'}
        with mock.patch.object(H, "PAGE", 2):
            self.assertEqual(len(h.current()["data"]["alerts"]), 4)
        self.assertEqual(offsets, [(0, True), (2, False), (4, False)])          # only the first page is conditional


class EndpointTest(unittest.TestCase):
    def test_the_map_gets_alerts_and_a_popup_its_text(self):
        cfg = from_dict({**CFG, "database": os.path.join(tempfile.mkdtemp(), "db.duckdb"), "web": {"allowed_hosts": ["testserver"]}})
        c = TestClient(create_app(cfg, FakeFlickr(), scheduler=False), base_url="https://testserver")
        nws = {"properties": {"event": "Tornado Warning", "headline": "Tornado Warning until 3 PM", "areaDesc": "Rio Grande, CO",
                              "senderName": "NWS Pueblo CO", "description": "...", "instruction": "Take cover now."}}
        asked = []
        def get(url, params=None, headers=None, timeout=None):
            asked.append(url)
            return answer(FEATURES) if params else mock.Mock(raise_for_status=lambda: None, json=lambda: nws)
        with mock.patch.object(H, "hazards", H.Hazards(get=get, clock=lambda: NOW, background=False)):
            d = c.get("/api/wx/hazards?zoom=4").json()
            self.assertEqual((d["error"], d["pending"], d["shapes"]["level"], d["shapes"]["alerts"][0]["event"]), (None, False, "coarse", "Tornado Warning"))
            self.assertEqual(c.get("/api/wx/hazards?bbox=-107,36,-103,39&zoom=8").json()["shapes"]["level"], "fine")
            text = c.get("/api/wx/hazards/alert", params={"id": "urn:tor"}).json()
            self.assertEqual((text["area"], text["instruction"]), ("Rio Grande, CO", "Take cover now."))
            c.get("/api/wx/hazards/alert", params={"id": "urn:tor"})
            self.assertEqual(asked.count(H.DETAIL_URL.format("urn:tor")), 1)       # kept a few minutes
            self.assertEqual(c.get("/api/wx/hazards/alert", params={"id": "https://example.com"}).status_code, 404)   # not a proxy
            self.assertEqual(c.get("/api/wx/hazards?bbox=1,2").status_code, 422)


if __name__ == "__main__":
    unittest.main()
