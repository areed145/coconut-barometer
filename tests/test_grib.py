"""GRIB2 (weather/grib.py) and Lambert grids (weather/lambert.py): simple packing unpacked, sign-and-magnitude numbers,
RTMA's own grid read and projected, winds turned earth-relative (checked against RTMA's WDIR), and resampling onto a
lat/lon grid. Also the background refresh of a Feed (weather/feed.py); fronts test the rest of it."""
import calendar
import math
import struct
import time
import unittest
from unittest import mock

import numpy as np

from coconut_barometer.weather import feed, grib, lambert as L

T = calendar.timegm((2026, 10, 24, 19, 30, 0, 0, 0, 0))
# Section 3 of a real RTMA Rapid Update file (rtma2p5_ru.t1930z, 24 October 2026): the 2.5 km CONUS Lambert grid,
# 2345 × 1597, tangent at 25° N, LoV 265°, a sphere of 6 371 200 m, winds grid-relative, rows going north.
RTMA_SEC3 = bytes.fromhex(
    "000000510300003924c50000001e01000061378000000000000000000000000009290000063d012569300dee563808017d78400fcb9440"
    "0026c0b70026c0b70040017d7840017d7840855d4a8000000000")
# Grid-relative u, v (m/s) at a few of its points, and RTMA's WDIR (degrees, where the wind is from) there.
RTMA_WINDS = [("Seattle", 415, 1210, 0.31, -0.2, 291.5, -122.33), ("Boston", 2097, 948, -1.23, 1.6, 152.6, -71.06),
              ("San Diego", 466, 509, 3.19, 0.97, 243.7, -117.2), ("Maine", 2166, 1133, 1.54, -1.3, 321.7, -68.0)]


def pack_bits(x: np.ndarray, bits: int) -> bytes:
    out = np.unpackbits(x.astype(">u4").view(np.uint8).reshape(-1, 4), axis=1)[:, 32 - bits:]
    return np.packbits(out.ravel()).tobytes()


def simple(values: np.ndarray, sec3: bytes, field=(2, 2), ref=-2000.0, d=2, bits=13, t=T) -> bytes:
    """A GRIB2 message of `values` (float, one row per grid row), simple-packed (5.0): X = value·10^d − ref."""
    x = np.round(values.ravel() * 10 ** d - ref).astype(np.int64)
    y, mo, dd, h, mi, se = time.gmtime(t)[:6]
    sec1 = struct.pack(">IB4xBBB", 21, 1, 2, 1, 1) + struct.pack(">H5B", y, mo, dd, h, mi, se) + b"\0\0"
    sec4 = bytearray(34)
    sec4[0:5] = struct.pack(">IB", 34, 4)
    sec4[9:11] = bytes(field)
    sec5 = struct.pack(">IBIH", 21, 5, x.size, 0) + struct.pack(">f", ref) + struct.pack(">HHBB", 0, d, bits, 0)
    sec6 = struct.pack(">IBB", 6, 6, 255)
    data = pack_bits(x, bits)
    sec7 = struct.pack(">IB", 5 + len(data), 7) + data
    body = sec1 + sec3 + bytes(sec4) + sec5 + sec6 + sec7 + b"7777"
    return b"GRIB\0\0\0\x02" + struct.pack(">Q", 16 + len(body)) + body


def small_sec3(nx: int, ny: int) -> bytes:
    """RTMA's grid section, cut to nx × ny points from the same corner."""
    s = bytearray(RTMA_SEC3)
    s[30:38] = struct.pack(">II", nx, ny)
    return bytes(s)


class GribTest(unittest.TestCase):
    def test_sign_and_magnitude(self):
        self.assertEqual(grib.signed(0x8004, 16), -4)                   # HRRR's binary scale factor, not -32764
        self.assertEqual(grib.signed(0x0004, 16), 4)
        self.assertEqual(grib.signed(0x80000001, 32), -1)

    def test_bits_unpack_at_every_width_and_across_chunks(self):
        rng = np.random.default_rng(1)
        for bits in (1, 7, 8, 12, 13, 16, 25):
            x = rng.integers(0, 1 << bits, 1000, dtype=np.int64)
            with mock.patch.object(grib, "CHUNK", 37):
                np.testing.assert_array_equal(grib.unpack_bits(pack_bits(x, bits), bits, x.size), x)
        np.testing.assert_array_equal(grib.unpack_bits(b"", 0, 4), [0, 0, 0, 0])   # a constant field
        with self.assertRaises(ValueError):
            grib.unpack_bits(b"\0" * 8, 26, 1)

    def test_rtma_grid_and_a_simple_packed_field(self):
        u = np.array([[0.31, -0.2, 3.0], [-1.23, 1.6, 19.45]], dtype=np.float32)
        (msg,) = grib.messages(simple(u, small_sec3(3, 2)))
        g = msg["grid"]
        self.assertEqual((msg["t"], msg["field"], g["template"], g["nx"], g["ny"]), (T, (0, 2, 2), 30, 3, 2))
        self.assertEqual((g["la1"], round(g["lo1"], 6), g["lov"], g["latin1"], g["radius"]), (19.228976, -126.276552, -95.0, 25.0, 6371200.0))
        self.assertEqual((g["dx"], g["grid_relative"], g["south_first"]), (2539.703, True, True))
        np.testing.assert_allclose(grib.values(msg), u, atol=0.005)

    def test_back_to_back_messages_and_what_isnt_read(self):
        a, b = np.zeros((2, 3)), np.ones((2, 3))
        both = grib.messages(simple(a, small_sec3(3, 2)) + simple(b, small_sec3(3, 2), field=(2, 3)))
        self.assertEqual([m["field"] for m in both], [(0, 2, 2), (0, 2, 3)])
        np.testing.assert_allclose(grib.values(both[1]), b)
        with self.assertRaises(ValueError):
            grib.messages(b"not grib")
        complex_ = bytearray(simple(a, small_sec3(3, 2)))
        at = complex_.index(struct.pack(">IBIH", 21, 5, 6, 0))
        complex_[at + 9:at + 11] = struct.pack(">H", 3)                   # complex packing (5.3)
        with self.assertRaisesRegex(ValueError, "5.3"):
            grib.message(bytes(complex_))


class LambertTest(unittest.TestCase):
    def setUp(self):
        self.lam = L.Lambert(grib.message(simple(np.zeros((1, 1)), small_sec3(1, 1)))["grid"] | {"nx": 2345, "ny": 1597})

    def test_the_first_point_is_the_grid_corner_and_cities_land_where_rtma_has_them(self):
        np.testing.assert_allclose(self.lam.ij(19.228976, -126.276552), (0, 0), atol=1e-6)
        # Denver, Miami: the points whose temperatures read 52 °F and 79 °F on that afternoon.
        self.assertEqual(tuple(round(float(x)) for x in self.lam.ij(39.74, -104.99)), (940, 770))
        self.assertEqual(tuple(round(float(x)) for x in self.lam.ij(25.76, -80.19)), (1871, 169))
        # On LoV, a degree of latitude is ~111 km: ~43.8 grid steps of 2539.7 m (conformal, so the scale is the
        # same both ways; at 40° N the cone's scale is ~1.03 against the tangent 25°).
        steps = float(self.lam.ij(41.0, -95.0)[1] - self.lam.ij(40.0, -95.0)[1])
        self.assertAlmostEqual(steps, 111.2e3 / 2539.703 * 1.03, delta=0.5)

    def test_winds_turned_earth_relative_agree_with_rtma_wdir(self):
        for name, _i, _j, ug, vg, wdir, lon in RTMA_WINDS:
            ue, ve = self.lam.earth_relative(np.float32(ug), np.float32(vg), lon)
            frm = math.degrees(math.atan2(-float(ue), -float(ve))) % 360
            self.assertLess(abs((frm - wdir + 180) % 360 - 180), 1.5, name)
        ue, ve = self.lam.earth_relative(np.float32(0), np.float32(1), -95.0)      # at LoV: grid up is north
        self.assertEqual((round(float(ue), 6), round(float(ve), 6)), (0, 1))

    def test_resampled_onto_lat_lon(self):
        lam = self.lam
        _jj, ii = np.mgrid[0:1597, 0:2345].astype(np.float32)
        box = L.latlon_grid(38.0, 42.0, -107.0, -103.0, 0.5)
        self.assertEqual((box["nx"], box["ny"]), (9, 9))
        cols = L.resample(lam, ii, box)                                   # a field of its own column numbers ...
        lats, lons = L.points(box)
        np.testing.assert_allclose(cols, lam.ij(lats, lons)[0], atol=1e-3)   # ... reads back where each point is
        outside = L.resample(lam, ii, L.latlon_grid(60.0, 61.0, -20.0, -19.0, 0.5))
        self.assertTrue(np.isnan(outside).all())                          # off the grid: NaN, not its edge


class BackgroundFeedTest(unittest.TestCase):
    def test_a_slow_feed_answers_at_once_and_fills_in(self):
        later, now = [], [1000.0]

        class Slow(feed.Feed):
            what = "slow"

            def load(self, a):
                return {"n": len(later)}
        f = Slow(clock=lambda: now[0], ttl=60, background=True, spawn=later.append)
        first = f.current()
        self.assertEqual((first["data"], first["pending"]), (None, True))   # nothing yet: ask again soon
        self.assertEqual(f.current()["pending"], True)
        self.assertEqual(len(later), 1)                                     # one refresh at a time
        later.pop()()
        done = f.current()
        self.assertEqual((done["data"], done["pending"], done["error"]), ({"n": 0}, False, None))
        now[0] += 60
        self.assertEqual(f.current()["data"], {"n": 0})                     # stale: kept while it refreshes
        self.assertEqual(len(later), 1)


if __name__ == "__main__":
    unittest.main()
