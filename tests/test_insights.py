"""The Analytics tabs' numbers (insights.py) on a small library with known answers."""
import datetime as dt
import json
import os
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from fakes import FakeFlickr, CFG
from coconut_barometer import insights as I
from coconut_barometer.config import from_dict
from coconut_barometer.store import Store
from coconut_barometer.sync import sync
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import hash_password

PASSWORD = "correct horse battery"
DAY = 86400


class InsightsTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        self.db = Store(self.path)
        sync(FakeFlickr(), self.db, log=lambda *a: None)
        now = int(time.time())
        x = self.db.db.execute
        # Photo 1 is the hit; all were uploaded the same year so "typical" is the median.
        for pid, views, faves, tags in (("1", 5000, 100, "ramen tokyo y2019 me"), ("2", 100, 1, "austin me"),
                                         ("3", 120, 2, "houston me"), ("4", 80, 0, "me"), ("5", 90, 9, "austin me")):
            x("UPDATE photos SET views=?, faves=?, comments=?, tags=?, date_upload=? WHERE id=?",
              (views, faves, 1, tags, now - 400 * DAY, pid))
        today = I.today()
        for back in range(1, 29):                       # 4 weeks of daily stats
            day = (today - dt.timedelta(days=back)).isoformat()
            for pid in "12345":
                v = 20 if pid == "1" and back <= 14 else 2   # photo 1 doubles... and more, lately
                x("INSERT INTO photo_daily VALUES (?, ?, ?, 0, 0)", (pid, day, v))
        x("INSERT INTO decisions VALUES ('1', 'add_tag', 'ramen', 'applied', '', ?)", (now - 20 * DAY,))
        x("INSERT INTO cameras VALUES ('1', 'Nikon D90', 'Nikon', 'D90', 0, '18-55mm', ?)",
          (json.dumps({"FocalLengthIn35mmFormat": "27 mm", "FNumber": "f/2.8", "ISO": "200",
                       "DateTimeOriginal": "2019:07:17 12:03:25"}),))
        for i, (uid, ago_days) in enumerate((("u1", 400), ("u1", 1), ("u2", 1))):
            x("INSERT INTO faves VALUES (?, ?, ?, '', ?)", (str(i + 1), uid, uid, now - ago_days * DAY))
        self.db.db.commit()

    def test_tags_hide_dates_and_tags_on_most_photos(self):
        t = {a["tag"]: a for a in I.tag_table(self.db, min_photos=1)["tags"]}
        self.assertNotIn("y2019", t)                                    # a date tag
        self.assertEqual(t["me"]["kind"], "subject")                    # "common" needs 20+ photos
        I.COMMON, saved = 0.3, I.COMMON
        self.db.db.executemany("INSERT INTO photos (id, tags, views, date_upload) VALUES (?, 'me', 1, 0)",
                               [(f"x{i}",) for i in range(20)])
        self.assertNotIn("me", {a["tag"] for a in I.tag_table(self.db, min_photos=1)["tags"]})
        self.assertIn("me", {a["tag"] for a in I.tag_table(self.db, hide=(), min_photos=1)["tags"]})
        self.db.db.execute("DELETE FROM photos WHERE id LIKE 'x%'")
        self.assertGreater(t["ramen"]["lift"], 10)                      # the hit
        self.assertEqual(t["austin"]["photos"], 2)
        self.assertEqual(t["ramen"]["trend"], 9.0)                      # 20/day against 2/day
        added = {a["tag"] for a in I.tag_table(self.db, source="added", min_photos=1)["tags"]}
        self.assertEqual(added, {"ramen"})
        self.assertNotIn("ramen", {a["tag"] for a in I.tag_table(self.db, source="yours", min_photos=1)["tags"]})

    def test_tag_detail(self):
        d = I.tag_detail(self.db, "Austin")
        self.assertEqual((d["photos"], d["top"]), (2, ["2", "5"]))
        self.assertEqual(d["often_with"][0]["tag"], "me")
        self.assertEqual(len(d["series"]), 28)

    def test_photo_lists(self):
        self.assertEqual(I.photo_lists(self.db, "views", 3650)[0]["photo_id"], "1")
        self.assertEqual(I.photo_lists(self.db, "engagement", 30, min_views=50)[0]["photo_id"], "5")
        # Sleepers: old photos far busier lately than their lifetime average. Photo 4 (80 views
        # in 400 days, 2 a day lately) beats photo 1 (5,000 views: busy, but always was).
        self.assertEqual(I.photo_lists(self.db, "sleepers", 30)[0]["photo_id"], "4")

    def test_gear_and_light(self):
        g = I.gear(self.db)
        self.assertEqual(g["focal"][0]["label"], "20–28 mm")
        self.assertEqual([r["label"] for r in g["aperture"]], ["f/2–2.8"])
        self.assertEqual(g["hour"][0]["label"], "12")
        self.assertGreater(I.sun_altitude(0, 0, dt.datetime(2024, 3, 20, 12)), 80)       # equinox noon, equator
        self.assertLess(I.sun_altitude(0, 0, dt.datetime(2024, 3, 20, 0)), -60)
        self.assertEqual(I.light(3), "golden hour")

    def test_audience(self):
        a = I.audience(self.db)
        self.assertEqual((a["people"], len(a["week_utc"])), (2, 168))
        self.assertEqual(a["favers"][0]["user_id"], "u1")
        self.assertEqual(sum(a["week_utc"]), 2)                         # the last year only
        self.assertEqual(sum(r["faves"] for r in a["timing"]), 3)

    def test_impact_against_unchanged_photos(self):
        x = self.db.db.execute
        x("DELETE FROM decisions")
        changed_at = int(time.mktime((I.today() - dt.timedelta(days=14)).timetuple())) + 3600
        x("INSERT INTO decisions VALUES ('1', 'set_title', 'New', 'applied', '', ?)", (changed_at,))
        self.db.db.commit()
        titles = next(k for k in I.impact(self.db, 13)["kinds"] if k["kind"] == "Titles")
        self.assertEqual((titles["photos"], titles["counted"]), (1, 1))
        self.assertAlmostEqual(titles["change"], 9.0, places=1)         # 2/day before, 20/day after
        self.assertEqual(titles["control_change"], 0)                   # the other photos stayed flat
        self.assertIsNone(titles["effect"])                             # 4 unchanged photos: too few to call it

    def test_api(self):
        self.db.set_password_hash("owner", hash_password(PASSWORD))
        cfg = from_dict({**CFG, "database": self.path, "web": {"allowed_hosts": ["testserver"]}})
        c = TestClient(create_app(cfg, None, scheduler=False), base_url="https://testserver")
        self.assertEqual(c.get("/api/analytics/tags").status_code, 401)
        c.post("/api/auth/login", json={"username": "owner", "password": PASSWORD})
        for path in ("overview?days=30", "photos?kind=sleepers", "photos?kind=gaining", "photos?kind=overlooked",
                     "tags?sort=lift&hide=date,place", "tags/austin", "places?level=city", "gear", "audience",
                     "groups", "impact"):
            r = c.get("/api/analytics/" + path)
            self.assertEqual(r.status_code, 200, (path, r.text[:200]))
        o = c.get("/api/analytics/overview", params={"days": 7}).json()
        self.assertEqual(o["period"]["views"], o["period"]["views"])    # shape
        self.assertIn("highlights", o)
        self.assertEqual(c.get("/api/analytics/photos", params={"kind": "nope"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
