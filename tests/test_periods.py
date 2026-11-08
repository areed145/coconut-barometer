"""Periods (periods.py): the last N days, or two dates."""
import datetime as dt
import unittest

from coconut_barometer import periods as P

TODAY = dt.date(2026, 11, 8)


class Custom(unittest.TestCase):
    def test_none_without_dates(self):
        self.assertIsNone(P.custom(None, None, TODAY))
        self.assertIsNone(P.custom("", "", TODAY))

    def test_both_days_count_and_the_end_stops_at_today(self):
        d = P.custom("2026-11-01", "2026-12-31", TODAY)
        self.assertEqual((d.start, d.end, d.days), (dt.date(2026, 11, 1), TODAY, 8))

    def test_the_period_before_is_as_long(self):
        d = P.custom("2026-03-01", "2026-03-31", TODAY).before()
        self.assertEqual((d.start, d.end), (dt.date(2026, 1, 29), dt.date(2026, 2, 28)))

    def test_what_isnt_a_period(self):
        for start, end in (("2026-01-01", None), ("2026-02-01", "2026-01-01"), ("2026-13-01", "2026-12-01"),
                           ("2026-12-01", "2026-12-31"), ("1900-01-01", "2026-01-01")):
            with self.assertRaises(P.BadPeriod, msg=(start, end)):
                P.custom(start, end, TODAY)


class Window(unittest.TestCase):
    def test_the_last_n_days_all_and_dates(self):
        now = 2_000_000_000
        self.assertEqual(P.window(7, None, dt.timezone.utc, now), {"since": now - 7 * 86400, "until": None, "before": now - 14 * 86400, "days": 7})
        self.assertEqual(P.window(P.ALL, None, dt.timezone.utc, now), {"since": 0, "until": None, "before": None, "days": P.ALL})
        w = P.window(7, P.Dates(dt.date(2026, 1, 1), dt.date(2026, 1, 2)), dt.timezone.utc, now)
        jan1 = int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual(w, {"since": jan1, "until": jan1 + 2 * 86400, "before": jan1 - 2 * 86400, "days": 2})
        self.assertEqual(P.within([{"played_at": jan1}, {"played_at": jan1 + 2 * 86400}], w), [{"played_at": jan1}])


if __name__ == "__main__":
    unittest.main()
