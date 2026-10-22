"""The Weather map: which radar and satellite frames exist (fetched from IEM and NASA GIBS, faked here),
caching, a provider being down, and the endpoint and CSP that let the browser load the tiles."""
import unittest

from coconut_barometer.weather.imagery import Imagery, expand_periods
from coconut_barometer.web.security import CSP

DOMAIN = ("<Domains><DimensionDomain><ows:Identifier>time</ows:Identifier>"
          "<Domain>2026-10-21T20:00:00Z/2026-10-21T21:00:00Z/PT10M,2026-10-21T22:00:00Z/2026-10-21T22:00:00Z/PT10M</Domain>"
          "</DimensionDomain></Domains>")


class Resp:
    def __init__(self, body=None, text="", status=200):
        self.body, self.text, self.status = body, text, status

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class FakeGet:
    def __init__(self, radar_down=False):
        self.calls, self.radar_down = [], radar_down

    def __call__(self, url, timeout=None, params=None):
        self.calls.append(url)
        if "n0q_0.json" in url:
            if self.radar_down:
                raise ConnectionError("no route to host")
            return Resp({"meta": {"valid": "2026-10-21T22:45:00Z"}})
        return Resp(text=DOMAIN)


class ImageryTest(unittest.TestCase):
    def test_periods_expand_to_every_frame(self):
        self.assertEqual(len(expand_periods("2026-10-21T20:00:00Z/2026-10-21T21:00:00Z/PT10M,2026-10-21T22:00:00Z")), 8)

    def test_radar_is_the_last_hour_at_five_minutes_and_satellite_the_newest_frames(self):
        f = Imagery(get=FakeGet(), clock=lambda: 1_792_620_000).frames()
        radar = f["radar"]["frames"]
        self.assertEqual(len(radar), 12)
        self.assertEqual((radar[0]["layer"], radar[-1]["layer"]), ("nexrad-n0q-900913-m55m", "nexrad-n0q-900913"))
        self.assertEqual(radar[-1]["t"] - radar[0]["t"], 55 * 60)
        geo = f["satellite"]["geocolor"]
        self.assertEqual([x["time"] for x in geo["frames"]][-2:], ["2026-10-21T21:00:00Z", "2026-10-21T22:00:00Z"])
        self.assertEqual(len(geo["frames"]), 8)
        self.assertIn("{time}", geo["url"])

    def test_the_time_range_reaches_gibs_unencoded(self):
        get = FakeGet()
        Imagery(get=get, clock=lambda: 1_792_620_000).frames()
        sat = [u for u in get.calls if "DescribeDomains" in u][0]
        self.assertRegex(sat, r"&time=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ/\d{4}-")

    def test_cached_between_refreshes(self):
        get, now = FakeGet(), [1_792_620_000]
        im = Imagery(get=get, ttl=240, clock=lambda: now[0])
        im.frames(); im.frames()
        self.assertEqual(len(get.calls), 3)            # radar + two satellite layers, once
        now[0] += 300
        im.frames()
        self.assertEqual(len(get.calls), 6)

    def test_a_provider_down_leaves_the_others(self):
        f = Imagery(get=FakeGet(radar_down=True), clock=lambda: 1_792_620_000).frames()
        self.assertEqual(f["radar"]["frames"], [])
        self.assertIn("no route", f["radar"]["error"])
        self.assertTrue(f["satellite"]["infrared"]["frames"])

    def test_csp_allows_the_tile_hosts_only_as_images(self):
        img = next(d for d in CSP.split(";") if d.strip().startswith("img-src"))
        for host in ("https://mesonet.agron.iastate.edu", "https://gibs.earthdata.nasa.gov"):
            self.assertIn(host, img)
        self.assertIn("connect-src 'self';", CSP)
