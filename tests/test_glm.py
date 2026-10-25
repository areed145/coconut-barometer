"""The Weather map's lightning (#94): GOES-East GLM flashes, listed on AWS and read from THREDDS as text (both faked
here), held for KEEP, fetched only when new, and counted per minute and cell for the map."""
import re
import unittest

from coconut_barometer.weather.glm import KEEP, LEVELS, Lightning, hour_prefix, listed, parse_ascii, start_time
# The smooth shapes' tests, retired (#146; below):
# import json
# import numpy as np
# from coconut_barometer.weather.glm import FRESH, TRAIL, shapes_text

NOW = 1_792_884_000          # 2026-10-24 23:20:00 UTC (day 297)


def name(t):
    """A GLM file name starting at epoch t."""
    import time
    g = time.gmtime(t)
    s = f"{g.tm_year}{g.tm_yday:03d}{g.tm_hour:02d}{g.tm_min:02d}{g.tm_sec:02d}0"
    return f"OR_GLM-L2-LCFA_G19_s{s}_e{s}_c{s}.nc"


def ascii_answer(flashes):
    lat = ", ".join(str(a) for a, _ in flashes)
    lon = ", ".join(str(b) for _, b in flashes)
    n = len(flashes)
    return (f"Dataset {{\n    Float32 flash_lat[number_of_flashes = {n}];\n}} x.nc;\n"
            f"---------------------------------------------\nflash_lat[{n}]\n{lat}\n\nflash_lon[{n}]\n{lon}\n\n")


class Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code, self.headers = text, status, {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeNoaa:
    """The bucket's listing (honoring prefix and start-after) and THREDDS's text answers. files: name -> flashes;
    late: names THREDDS doesn't have yet."""

    def __init__(self, files, late=()):
        self.files, self.late, self.listings, self.reads, self.broken = dict(files), set(late), [], [], False

    def __call__(self, url, params=None, headers=None, timeout=None):
        if "amazonaws" in url:
            self.listings.append(dict(params))
            keys = sorted(f"{hour_prefix(start_time(n))}{n}" for n in self.files)
            keys = [k for k in keys if k.startswith(params["prefix"]) and k > params.get("start-after", "")]
            return Resp("<ListBucketResult>" + "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys) + "</ListBucketResult>")
        n = re.search(r"/(OR_GLM[^/]+\.nc)\.ascii", url).group(1)
        self.reads.append(n)
        if self.broken:
            return Resp("Error { code = 500; };", 500)
        if n in self.late or n not in self.files:
            return Resp("Error { code = 404; };", 404)
        return Resp(ascii_answer(self.files[n]))


def feed(get, now):
    return Lightning(get=get, clock=lambda: now[0], background=False)


class GlmTest(unittest.TestCase):
    def test_names_and_listings(self):
        self.assertEqual(start_time("OR_GLM-L2-LCFA_G19_s20262972318000_e20262972318200_c20262972318218.nc"), NOW - 120)
        self.assertEqual(hour_prefix(NOW), "GLM-L2-LCFA/2026/297/23/")
        self.assertEqual(listed("<Key>GLM-L2-LCFA/2026/297/23/a.nc</Key><Key>x/b.nc</Key>"), ["a.nc", "b.nc"])
        with self.assertRaises(ValueError):
            start_time("not-a-glm-file.nc")

    def test_the_text_answer_parses(self):
        lat, lon = parse_ascii(ascii_answer([(36.16, -104.08), (-1.78, -79.93)]))
        self.assertEqual([round(float(x), 2) for x in lat], [36.16, -1.78])
        self.assertEqual([round(float(x), 2) for x in lon], [-104.08, -79.93])
        with self.assertRaises(ValueError):
            parse_ascii("Dataset {} x;\n---\nflash_lat[3]\n1.0, 2.0\n\nflash_lon[3]\n1.0, 2.0, 3.0\n")   # short
        with self.assertRaises(ValueError):
            parse_ascii("Error { code = 500; };")

    def test_held_for_keep_and_fetched_once(self):
        now = [NOW]
        old, recent, newest = name(NOW - KEEP - 60), name(NOW - 600), name(NOW - 60)
        noaa = FakeNoaa({old: [(30, -100)], recent: [(35.0, -97.0), (35.01, -97.01)], newest: [(40, -90)]})
        g = feed(noaa, now)
        s = g.current()
        self.assertIsNone(s["error"])
        self.assertEqual(sorted(noaa.reads), sorted([recent, newest]))      # the one past KEEP isn't read
        self.assertEqual(noaa.reads[0], newest)                              # the newest first
        self.assertEqual(len(s["data"]["t"]), 3)
        self.assertEqual(s["data"]["newest"], NOW - 60)
        # The next round lists only past what it's seen, and reads only the new file.
        later = name(NOW + 20)
        noaa.files[later] = [(41, -91)]
        now[0] += 61
        g.current()
        self.assertEqual(noaa.reads[-1], later)
        self.assertEqual(len(noaa.reads), 3)
        self.assertTrue(any("start-after" in p for p in noaa.listings[-3:]))
        # Long after, everything has aged out.
        now[0] += KEEP + 60
        self.assertEqual(len(g.current()["data"]["t"]), 0)

    def test_a_file_thredds_lacks_is_tried_again(self):
        now, late = [NOW], name(NOW - 40)
        noaa = FakeNoaa({name(NOW - 60): [(35, -97)], late: [(36, -98)]}, late={late})
        g = feed(noaa, now)
        self.assertEqual(len(g.current()["data"]["t"]), 1)
        noaa.late.clear()
        now[0] += 61
        self.assertEqual(len(g.current()["data"]["t"]), 2)                   # listed before, read now
        self.assertEqual(noaa.reads.count(late), 2)

    def test_thredds_down_keeps_what_was_held(self):
        now = [NOW]
        noaa = FakeNoaa({name(NOW - 60): [(35, -97)]})
        g = feed(noaa, now)
        g.current()
        noaa.files[name(NOW + 20)] = [(36, -98)]
        noaa.late = set(noaa.files)
        now[0] += 61
        self.assertIsNone(g.current()["error"])                              # only late: not an error
        noaa.late, noaa.broken = set(), True
        now[0] += 61
        s = g.current()
        self.assertIn("none of 1 files", s["error"])
        self.assertEqual(len(s["data"]["t"]), 1)

    def test_the_view_counts_per_minute_and_cell(self):
        now = [NOW]
        a, b = name(NOW - 600), name(NOW - 590)      # the same minute
        c = name(NOW - 60)
        noaa = FakeNoaa({a: [(35.10, -97.13), (35.11, -97.14)], b: [(35.12, -97.15), (45.0, -60.0)], c: [(35.5, -97.5)]})
        g = feed(noaa, now)
        v = g.view((-100, 30, -90, 40), zoom=4)
        self.assertEqual((v["level"], v["q"]), ("coarse", LEVELS["coarse"]))
        minutes = dict((m, cells) for m, cells in v["minutes"])
        self.assertEqual(sorted(minutes), [NOW - 600, NOW - 60])
        self.assertEqual(minutes[NOW - 600], [351, -972, 3])                 # three in one ~11 km cell; 45 N 60 W is out of the box
        self.assertEqual(minutes[NOW - 60], [355, -975, 1])
        self.assertEqual([m for m, _ in g.view((-100, 30, -90, 40), 4, since=NOW - 300)["minutes"]], [NOW - 60])
        fine = g.view((-100, 30, -90, 40), zoom=7)
        self.assertEqual(fine["level"], "fine")
        self.assertEqual(len(dict(fine["minutes"])[NOW - 600]) // 3, 1)      # within a ~3 km cell still
        self.assertEqual(g.view((0, 0, 10, 10), 4)["minutes"], [])


# The smooth shapes (#101), retired with the map's Smooth lightning (#146): kept with the code they test.
# def flashes(*fs):
#     """(age seconds before NOW, lat, lon) -> Lightning.data as it holds them."""
#     return {"t": np.array([NOW - a for a, _, _ in fs], dtype=np.int64), "lat": np.array([round(la * 100) for _, la, _ in fs], dtype=np.int16),
#             "lon": np.array([round(lo * 100) for _, _, lo in fs], dtype=np.int16), "newest": NOW - min(a for a, _, _ in fs)}
#
#
# class ShapesTest(unittest.TestCase):
#     """#101: the smooth shapes, traced on the server as radar's are."""
#     BOX = (-100.0, 30.0, -94.0, 36.0)
#
#     def shapes(self, data, level="fine", box=BOX):
#         return json.loads(shapes_text(data, NOW, box, level))
#
#     def test_nothing_to_draw(self):
#         for data in (None, flashes((10, 33.0, -97.0)) | {"t": np.zeros(0, dtype=np.int64)},
#                      flashes((TRAIL + 60, 33.0, -97.0)),          # older than the trail
#                      flashes((10, 45.0, -60.0))):                 # nowhere near the view
#             out = self.shapes(data)
#             self.assertEqual(out["bands"], [])
#
#     def test_a_lone_strike_still_shows_and_says_how_recent(self):
#         new = self.shapes(flashes((20, 33.0, -97.0)))
#         self.assertEqual([b["age"] for b in new["bands"]], [round((1 - k / FRESH) * TRAIL) for k in range(FRESH)])
#         self.assertEqual([len(b["polys"]) for b in new["bands"]], [1] * FRESH)       # one place, every level of recent
#         w, s_, e, n = new["bands"][0]["polys"][0][:4]
#         q = new["q"]
#         self.assertTrue(w / q < -97.0 < e / q and s_ / q < 33.0 < n / q)
#         self.assertLess((e - w) / q, 0.5)                                             # about a GLM pixel across
#         old = self.shapes(flashes((TRAIL - 60, 33.0, -97.0)))
#         self.assertEqual([b["age"] for b in old["bands"]], [TRAIL])                   # there, but only as long ago
#
#     def test_no_contour_lines_only_the_recency_levels(self):
#         crowd = flashes(*[(30 + i, 33.0 + (i % 5) * 0.01, -97.0 + (i // 5) * 0.01) for i in range(40)])
#         out = self.shapes(crowd)
#         self.assertEqual(set(out), {"t", "level", "q", "bands"})                    # nothing traced for lines
#         self.assertTrue(all(set(b) == {"age", "polys"} for b in out["bands"]))
#
#     def test_a_strike_just_outside_the_view_still_reaches_in(self):
#         edge = self.shapes(flashes((20, 33.0, self.BOX[2] + 0.03)))                  # ~3 km east of the view
#         self.assertTrue(edge["bands"])
#         far = self.shapes(flashes((20, 33.0, self.BOX[2] + 1.0)))
#         self.assertEqual(far["bands"], [])
#
#     def test_kept_per_minute_but_the_live_one_follows_new_flashes(self):
#         g = Lightning(get=None, clock=lambda: NOW)
#         g.data = flashes((90, 33.0, -97.0))
#         past = g.shapes(NOW - 600, self.BOX, 8)
#         g.data = flashes((90, 33.0, -97.0), (100, 33.5, -96.0))                     # a late file for that past
#         self.assertIs(g.shapes(NOW - 600, self.BOX, 8), past)                         # a past minute: kept
#         live = g.shapes(NOW, self.BOX, 8)
#         g.data = flashes((20, 34.0, -95.0), (90, 33.0, -97.0))                       # a newer file comes in
#         self.assertIsNot(g.shapes(NOW, self.BOX, 8), live)
#         self.assertEqual(json.loads(g.shapes(NOW, self.BOX, 3))["level"], "coarse")

if __name__ == "__main__":
    unittest.main()
