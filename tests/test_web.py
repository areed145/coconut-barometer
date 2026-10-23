"""The web app: login and security, then the gallery, review, sync and analytics APIs."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, fake_geocoder, CFG
from coconut_barometer.config import from_dict
from coconut_barometer.store import Store
from coconut_barometer.sync import sync
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import COOKIE, hash_password, token_hash

PASSWORD = "correct horse battery"


class WebTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        self.fl = FakeFlickr()
        store = Store(path)
        sync(self.fl, store, log=lambda *a: None)
        store.set_password_hash("owner", hash_password(PASSWORD))
        self.store = store
        self.cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]}})
        self.app = create_app(self.cfg, self.fl, provider_factory=lambda: FakeVision("m1"), scheduler=False,
                              process_kw={"geocoder": fake_geocoder, "downloader": lambda u: (b"jpg", "image/jpeg")})
        self.c = TestClient(self.app, base_url="https://testserver")

    def login(self, password=PASSWORD):
        return self.c.post("/api/auth/login", json={"username": "owner", "password": password})

    def csrf(self):
        return {"X-CSRF-Token": self.login().json()["csrf"]}

    def wait(self):
        self.app.state.runtime.review.wait()

    def test_photos_filter_by_tag_year_camera_and_album(self):
        x = self.store.db.execute
        x("UPDATE photos SET tags='ramen tokyo', date_taken='2019-07-17 10:00:00' WHERE id='1'")
        x("UPDATE photos SET tags='austin noodlesoup', date_taken='2024-01-01 00:00:00' WHERE id='2'")
        x("INSERT OR REPLACE INTO cameras (photo_id, camera) VALUES ('2', 'Apple iPhone X')")
        self.store.db.commit()
        self.login()
        ids = lambda **p: [ph["id"] for ph in self.c.get("/api/photos", params=p).json()["photos"]]
        self.assertEqual(ids(tag="ramen"), ["1"])
        self.assertEqual(ids(tag="Noodle Soup"), ["2"])                  # matched in Flickr's normalized form
        self.assertEqual(ids(tag="ram"), [])                             # whole tags only
        self.assertEqual(ids(year=2019), ["1"])
        self.assertEqual(ids(camera="Apple iPhone X"), ["2"])
        self.assertEqual(ids(tag="ramen", year=2024), [])                # filters combine
        album, members = next((a, ids_) for a, ids_ in self.store.db.execute(
            "SELECT album_id, group_concat(photo_id) FROM memberships GROUP BY album_id"))
        self.assertEqual(set(ids(album=album, limit=200)), set(members.split(",")))
        years = self.c.get("/api/photo-years").json()
        self.assertIn({"year": 2019, "count": 1}, years)
        self.assertEqual(self.c.get("/api/photos", params={"year": 99}).status_code, 422)

    def test_photo_presets(self):
        x = self.store.db.execute
        x("UPDATE photos SET title='IMG_1234', description='', tags='a b' WHERE id='2'")
        x("UPDATE photos SET views=5000, faves=3 WHERE id='1'")
        x("UPDATE photos SET lat=NULL, lon=NULL WHERE id='3'")
        x("INSERT INTO queue (photo_id, status, changes) VALUES ('5', 'pending', '[]')")
        self.store.db.commit()
        self.login()
        ids = lambda preset, **p: {ph["id"] for ph in self.c.get("/api/photos", params={"is": preset, **p}).json()["photos"]}
        self.assertIn("2", ids("untitled"))
        self.assertNotIn("1", ids("untitled"))                          # "Ramen in Tokyo"
        self.assertIn("2", ids("few_tags"))
        self.assertEqual(ids("popular"), {"1"})
        self.assertIn("3", ids("no_geotag"))
        self.assertEqual(ids("to_review"), {"5"})
        self.assertEqual(ids("untitled,few_tags"), ids("untitled") & ids("few_tags"))   # all must hold
        self.assertEqual(ids("popular", tag="nope"), set())
        self.assertEqual(self.c.get("/api/photos", params={"is": "untitled,drop table"}).status_code, 422)
        counts = {p["id"]: p for p in self.c.get("/api/photo-presets").json()}
        self.assertEqual(counts["popular"]["count"], 1)
        self.assertEqual(counts["popular"]["group"], "Engagement")

    def test_photos_on_this_day(self):
        x = self.store.db.execute
        x("UPDATE photos SET date_taken='2019-10-23 09:00:00', date_taken_unknown=0 WHERE id='1'")
        x("UPDATE photos SET date_taken='2024-10-23 18:30:00', date_taken_unknown=0 WHERE id='2'")
        x("UPDATE photos SET date_taken='2024-10-23 00:00:00', date_taken_unknown=1 WHERE id='3'")   # date unknown
        x("UPDATE photos SET date_taken='2024-10-24 08:00:00', date_taken_unknown=0 WHERE id='4'")
        self.store.db.commit()
        self.login()
        got = self.c.get("/api/photos", params={"on": "10-23", "sort": "taken"}).json()
        self.assertEqual([p["id"] for p in got["photos"]], ["2", "1"])              # newest taken first
        self.assertEqual(got["total"], 2)
        for bad in ("13-01", "10-32", "1023", "10-2"):
            self.assertEqual(self.c.get("/api/photos", params={"on": bad}).status_code, 422, bad)

    def test_photo_highlights_count_public_photos_for_visitors(self):
        x = self.store.db.execute
        x("UPDATE photos SET date_taken='2023-05-01 10:00:00', date_taken_unknown=0, ispublic=1, views=50, date_upload=2000 WHERE id='1'")
        x("UPDATE photos SET date_taken='2023-06-01 10:00:00', date_taken_unknown=0, ispublic=0, views=900, date_upload=2000 WHERE id='2'")
        self.store.db.commit()
        mine, theirs = self.store.photo_highlights(2023, 1000), self.store.photo_highlights(2023, 1000, public_only=True)
        self.assertEqual((mine["taken"], mine["top_upload"]["id"]), (2, "2"))
        self.assertEqual((theirs["taken"], theirs["top_upload"]["id"]), (1, "1"))          # the private one stays out

    def test_static_files_are_revalidated(self):
        r = self.c.get("/static/js/main.js")
        self.assertEqual(r.headers["cache-control"], "no-cache")
        again = self.c.get("/static/js/main.js", headers={"If-None-Match": r.headers["etag"]})
        self.assertEqual(again.status_code, 304)                         # unchanged: nothing re-sent

    def test_js_and_css_are_versioned_and_kept_for_good(self):
        import re as _re
        from coconut_barometer.web import assets

        page = self.c.get("/").text
        v = assets.version()
        self.assertIn(f'src="/static/v/{v}/js/main.js"', page)
        self.assertIn(f'href="/static/v/{v}/css/app.css"', page)
        self.assertIn(f'href="/static/v/{v}/js/lib.js"', page)                  # the preloads too
        self.assertNotRegex(page, r'"/static/(js|css|vendor)/')                   # nothing left unversioned
        self.assertIn('href="/static/fonts/dm-sans-latin.woff2"', page)          # fonts and images aren't
        r = self.c.get(f"/static/v/{v}/js/main.js")
        self.assertEqual((r.status_code, r.headers["cache-control"]), (200, "public, max-age=31536000, immutable"))
        self.assertIn("lazy(", r.text)
        self.assertEqual(self.c.get(f"/static/v/{v}/js/main.js", headers={"If-None-Match": r.headers["etag"]}).status_code, 304)
        for bad in (f"/static/v/{'0' * 12}/js/main.js", f"/static/v/{v}/img/logo-96.png", f"/static/v/{v}/js/nope.js"):
            r = self.c.get(bad)
            self.assertEqual(r.status_code, 404, bad)
            self.assertNotIn("immutable", r.headers.get("cache-control", ""), bad)
        self.assertTrue(_re.fullmatch(r"[0-9a-f]{12}", v))

    def test_the_version_follows_the_files(self):
        from coconut_barometer.web import assets

        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "js"))
        f = os.path.join(tmp, "js", "a.js")
        orig = assets.STATIC
        self.addCleanup(setattr, assets, "STATIC", orig)
        self.addCleanup(assets._cache.update, sig=None, version="")
        assets.STATIC = Path(tmp)
        open(f, "w").write("export const a = 1;")
        first = assets.version()
        self.assertEqual(assets.version(), first)                                 # unchanged: same version
        open(f, "w").write("export const a = 22;")
        self.assertNotEqual(assets.version(), first)

    def test_responses_are_compressed_except_auth(self):
        from fastapi import FastAPI
        from fastapi.responses import PlainTextResponse
        from fastapi.testclient import TestClient

        from coconut_barometer.web.app import Compress

        r = self.c.get("/static/js/weather.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.headers.get("content-encoding"), "gzip")
        self.assertIn("export function Weather", r.text)                 # still the file, once decoded
        again = self.c.get("/static/js/weather.js", headers={"Accept-Encoding": "gzip", "If-None-Match": r.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        tiny = FastAPI()
        tiny.add_middleware(Compress)
        for path in ("/api/auth/session", "/api/photos"):
            tiny.add_api_route(path, lambda: PlainTextResponse("csrf " * 1000))
        tc = TestClient(tiny)
        self.assertNotIn("content-encoding", tc.get("/api/auth/session", headers={"Accept-Encoding": "gzip"}).headers)
        self.assertEqual(tc.get("/api/photos", headers={"Accept-Encoding": "gzip"}).headers.get("content-encoding"), "gzip")

    def test_theme(self):
        self.assertEqual(self.c.get("/api/theme").json(), {"colors": ["#f7d125", "#1dab45"]})   # public

    def test_status_jobs_and_activity_need_login(self):
        self.assertEqual(self.c.get("/api/status/jobs").status_code, 401)
        self.login()
        q = self.c.get("/api/status/jobs").json()
        self.assertEqual(set(q), {"running", "waiting", "line", "by_kind", "took", "done_in", "services", "recent"})
        self.assertEqual(self.c.get("/api/status/activity").status_code, 200)

    # ---- security -----------------------------------------------------------
    def test_private_pages_need_a_session(self):
        for path in ("/api/review/summary", "/api/analytics/overview", "/api/sync/status", "/api/status/activity", "/api/status/jobs",
                     "/api/admin/settings", "/api/auth/session"):
            self.assertEqual(self.c.get(path).status_code, 401, path)
        self.assertEqual(self.c.post("/api/sync/run", json={}).status_code, 401)
        self.assertEqual(self.c.post("/api/wx/sync").status_code, 401)
        self.assertEqual(self.c.post("/api/review/items/1/process").status_code, 401)
        page = self.c.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("script-src 'self'", page.headers["content-security-policy"])
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        self.assertIn("max-age", page.headers["strict-transport-security"])

    def test_visitors_see_only_public_photos(self):
        x = self.store.db.execute
        x("UPDATE photos SET ispublic=0 WHERE id='1'")
        x("INSERT INTO queue (photo_id, status, changes) VALUES ('2', 'pending', '[]')")
        self.store.db.commit()
        ids = lambda **p: {ph["id"] for ph in self.c.get("/api/photos", params=p).json()["photos"]}
        self.assertEqual(ids(), {"2", "3", "4", "5"})
        self.assertEqual(self.c.get("/api/photos/1").status_code, 404)
        self.assertEqual(self.c.get("/api/photos/2").status_code, 200)
        self.assertEqual(self.c.get("/api/photos", params={"is": "to_review"}).status_code, 422)   # app status: private
        self.assertNotIn("Status", {p["group"] for p in self.c.get("/api/photo-presets").json()})
        self.assertNotIn("1", {c["id"] for c in self.c.post("/api/map/cards", json={"ids": ["1", "2"]}).json()})
        self.assertNotIn("1", {p[0] for p in self.c.get("/api/map/points").json()["points"]})
        for a in self.c.get("/api/albums").json():
            self.assertNotIn("1", {p["id"] for p in self.c.get(f"/api/albums/{a['id']}").json()["photos"]})
        # An album with a private photo as its cover, and in its count (both from Flickr): neither shown.
        album, public = x("SELECT album_id, COUNT(*) FROM memberships WHERE photo_id<>'1' GROUP BY album_id "
                          "ORDER BY album_id LIMIT 1").fetchone()
        x("INSERT OR IGNORE INTO memberships (photo_id, album_id) VALUES ('1', ?)", (album,))
        x("UPDATE albums SET cover_url='https://live.staticflickr.com/1/1_abc.jpg', count=99 WHERE id=?", (album,))
        self.store.db.commit()
        seen = next(a for a in self.c.get("/api/albums").json() if a["id"] == album)
        self.assertNotIn("/1_", seen["cover_url"])
        self.assertEqual(seen["count"], public)
        self.assertEqual(self.c.get(f"/api/albums/{album}").json()["count"], public)
        x("UPDATE photos SET geo_public=0 WHERE id='2'")              # public photo, location hidden on Flickr
        x("UPDATE photos SET geo_public=NULL WHERE id='3'")           # not synced since: unknown, so hidden too
        self.store.db.commit()
        self.assertEqual(self.c.get("/api/photos/2").json()["lat"], None)
        self.assertEqual(self.c.get("/api/photos/3").json()["lon"], None)
        self.assertIsNotNone(self.c.get("/api/photos/5").json()["lat"])
        self.assertEqual({p[0] for p in self.c.get("/api/map/points").json()["points"]}, {"5"})
        self.login()                                                     # signed in: everything
        self.assertEqual(ids(), {"1", "2", "3", "4", "5"})
        self.assertIsNotNone(self.c.get("/api/photos/2").json()["lat"])
        self.assertEqual(self.c.get("/api/photos/1").status_code, 200)
        self.assertEqual(ids(**{"is": "to_review"}), {"2"})

    def test_login_sets_a_strict_secure_cookie_and_stores_only_a_hash(self):
        self.assertEqual(self.login("wrong password!").status_code, 401)
        r = self.login()
        self.assertEqual(r.status_code, 200)
        cookie = r.headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=strict", "path=/"):
            self.assertIn(flag, cookie)
        token = self.c.cookies.get(COOKIE)
        stored = [row[0] for row in self.store.db.execute("SELECT token_hash FROM sessions")]
        self.assertEqual(stored, [token_hash(token)])
        self.assertNotIn(token, stored)
        self.assertEqual(self.c.get("/api/photos").json()["total"], 5)

    def test_re_read_every_photo_from_settings(self):
        r = self.c.post("/api/sync/run", json={"mode": "photo_list"}, headers=self.csrf())
        self.assertEqual(r.json()["status"], "queued")
        self.wait()
        self.assertEqual(self.c.get("/api/sync/status").json()["last_result"]["ran"], ["photo_list", "incremental"])

    def test_changes_need_the_csrf_token(self):
        self.login()
        self.assertEqual(self.c.post("/api/sync/run", json={}).status_code, 403)
        self.assertEqual(self.c.post("/api/sync/run", json={}, headers={"X-CSRF-Token": "guess"}).status_code, 403)
        self.assertEqual(self.c.post("/api/sync/run", json={}, headers=self.csrf()).status_code, 200)

    def test_failed_logins_are_throttled(self):
        for _ in range(5):
            self.assertEqual(self.login("wrong password!").status_code, 401)
        r = self.login()                                   # even the right one, for a while
        self.assertEqual(r.status_code, 429)
        self.assertGreater(int(r.headers["retry-after"]), 0)

    def test_foreign_host_headers_are_refused(self):
        self.assertEqual(self.c.get("/", headers={"Host": "evil.example"}).status_code, 400)

    def test_logout_and_password_change_end_sessions(self):
        headers = self.csrf()
        self.assertEqual(self.c.post("/api/auth/logout", headers=headers).status_code, 200)
        self.assertEqual(self.c.get("/api/review/summary").status_code, 401)
        self.login()
        self.store.set_password_hash("owner", hash_password("another long password"))
        self.assertEqual(self.c.get("/api/review/summary").status_code, 401)

    # ---- features -------------------------------------------------------------
    def test_gallery(self):
        self.login()
        page = self.c.get("/api/photos", params={"sort": "oldest", "limit": 2}).json()
        self.assertEqual((page["total"], len(page["photos"])), (5, 2))
        self.assertIn("b", page["photos"][0]["sizes"])
        self.assertEqual(self.c.get("/api/photos", params={"q": "ramen"}).json()["total"], 1)
        albums = self.c.get("/api/albums").json()
        texas = next(a for a in albums if a["title"] == "Texas")
        self.assertEqual(self.c.get(f"/api/albums/{texas['id']}").json()["total"], 1)
        detail = self.c.get("/api/photos/1").json()
        self.assertEqual(detail["title"], "Ramen in Tokyo")
        self.assertIn("engagement", detail)
        self.assertEqual(self.c.get("/api/photos/nope").status_code, 404)
        self.assertEqual(self.c.get("/api/photos", params={"sort": "drop table"}).status_code, 422)

    def test_bulk_replan_applied_and_undo_through_the_worker(self):
        h = self.csrf()
        self.c.post("/api/review/items/1/send", headers=h)
        self.wait()
        out = self.c.get("/api/review/bulk", params={"kind": "album", "value": "Food"}).json()
        picks = [{"photo_id": i["photo_id"], "index": i["index"], "action": i["change"]["action"]} for i in out["items"]]
        self.assertEqual(self.c.post("/api/review/bulk", json={"verb": "apply", "picks": picks}, headers=h).json()["changes"], 1)
        self.wait()
        self.assertIn("1", self.fl.sets["102"]["photos"])
        self.assertEqual(self.c.post("/api/review/replan", headers=h).json()["status"], "queued")
        self.wait()
        (row,) = self.c.get("/api/review/applied").json()
        self.assertTrue(row["can_undo"])
        self.assertEqual(self.c.post("/api/review/items/1/undo", headers=h).json()["status"], "undoing")
        self.wait()
        self.assertNotIn("1", self.fl.sets["102"]["photos"])
        self.assertEqual(self.c.post("/api/review/items/1/undo", headers=h).status_code, 404)   # nothing left
        self.assertEqual(self.c.post("/api/review/bulk", json={"verb": "wipe", "picks": []}, headers=h).status_code, 422)

    def test_review_send_apply_and_ignore_through_the_worker(self):
        headers = self.csrf()
        self.assertEqual(self.c.post("/api/review/items/1/send", headers=headers).json()["status"], "processing")
        self.wait()
        item = self.c.get("/api/review/items/1").json()
        self.assertEqual(item["status"], "pending")
        choices = [{"approve": c["new"] == "Food"} if c["action"] == "add_to_album" else {"approve": False}
                   for c in item["changes"]]
        r = self.c.post("/api/review/items/1/decide", json={"choices": choices}, headers=headers)
        self.assertEqual(r.json()["status"], "applying")
        self.wait()
        self.assertIn("1", self.fl.sets["102"]["photos"])                    # Food, on (fake) Flickr
        self.assertEqual(self.c.get("/api/review/items/1").json()["status"], "not_queued")   # out of review
        self.c.post("/api/review/items/2/send", headers=headers)
        self.wait()
        self.assertEqual(self.c.get("/api/review/queue").json(), ["2"])
        self.assertEqual(self.c.post("/api/review/items/2/ignore", headers=headers).json(), {"status": "ignored"})
        self.assertEqual(self.c.get("/api/review/queue").json(), [])
        self.assertEqual(self.c.get("/api/review/summary").json()["to_review"], 0)
        self.assertEqual(self.c.post("/api/review/items/2/ignore", headers=headers).status_code, 404)

    def test_send_to_review_again_uses_the_latest_image_and_asks_the_model_again(self):
        headers = self.csrf()
        seen = []
        self.app.state.runtime.review.process_kw["downloader"] = lambda u: (seen.append(u), (b"jpg", "image/jpeg"))[1]
        self.c.post("/api/review/items/1/process", headers=headers)
        self.wait()
        item = self.c.get("/api/review/items/1").json()
        self.c.post("/api/review/items/1/decide", json={"choices": [{"approve": True} for _ in item["changes"]]},
                    headers=headers)
        self.wait()
        self.fl.photos[0]["secret"] = "new"                                  # replaced on Flickr since
        self.c.post("/api/review/items/1/process", headers=headers)
        self.wait()
        self.assertEqual(seen, ["https://live.staticflickr.com/1/1_s.jpg", "https://live.staticflickr.com/1/1_new.jpg"])
        self.c.post("/api/review/items/2/process", params={"if_new": True}, headers=headers)   # look-ahead: no
        self.wait()                                                                              # extra calls
        self.assertEqual(len(seen), 2)

    def test_sync_now_and_status(self):
        headers = self.csrf()
        self.assertEqual(self.c.post("/api/sync/run", json={"mode": "incremental"}, headers=headers).json(),
                         {"status": "queued"})
        self.wait()
        status = self.c.get("/api/sync/status").json()
        self.assertEqual(status["last_result"]["ran"], ["incremental"])
        self.assertFalse(status["running"])
        self.assertEqual(self.c.post("/api/sync/run", json={"mode": "everything"}, headers=headers).status_code, 422)

    def test_scheduler_runs_whatever_is_due(self):
        rt = self.app.state.runtime
        rt.start_scheduler(first_delay=0)
        self.addCleanup(rt.stop)
        deadline = time.time() + 10
        while rt.store().get_meta("last_sync_result") is None and time.time() < deadline:
            time.sleep(0.05)
        rt.review.wait()
        self.assertIn("incremental", rt.sync_status()["last_result"]["ran"])
        self.assertGreater(rt.next_sync, time.time())

    def test_analytics(self):
        self.store.save_stats_day("2026-10-19", {"total": 50}, [{"photo_id": "1", "views": 40, "comments": 1,
                                                                  "favorites": 2}])
        self.login()
        top = self.c.get("/api/analytics/photos", params={"kind": "views", "days": 3000}).json()   # not "All": the daily stats
        self.assertEqual((top[0]["photo_id"], top[0]["value"]), ("1", 40))
        self.assertIn("highlights", self.c.get("/api/analytics/overview", params={"days": 3650}).json())
        self.assertEqual(self.c.get("/api/analytics/breakdown", params={"by": "year", "days": 3650}).json()[0]["views"], 40)
        self.assertEqual(self.c.get("/api/analytics/photos/1").json()["daily"][0]["views"], 40)
        self.assertEqual(self.c.get("/api/analytics/breakdown", params={"by": "secrets"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
