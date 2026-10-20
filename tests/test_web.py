"""The web app: login and security, then the gallery, review, sync and analytics APIs."""
import os
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, fake_geocoder, CFG
from flickr_organizer.config import from_dict
from flickr_organizer.store import Store
from flickr_organizer.sync import sync
from flickr_organizer.web.app import create_app
from flickr_organizer.web.security import COOKIE, hash_password, token_hash

PASSWORD = "correct horse battery"


class WebTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), "db.sqlite")
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

    def test_worker_view_needs_login(self):
        self.assertEqual(self.c.get("/api/review/worker").status_code, 401)
        self.login()
        q = self.c.get("/api/review/worker").json()
        self.assertEqual(set(q), {"running", "waiting", "flickr", "vision", "recent"})

    # ---- security -----------------------------------------------------------
    def test_everything_but_login_needs_a_session(self):
        for path in ("/api/photos", "/api/albums", "/api/review/summary", "/api/analytics/overview",
                     "/api/sync/status", "/api/auth/session"):
            self.assertEqual(self.c.get(path).status_code, 401, path)
        self.assertEqual(self.c.post("/api/sync/run", json={}).status_code, 401)
        page = self.c.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("script-src 'self'", page.headers["content-security-policy"])
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        self.assertIn("max-age", page.headers["strict-transport-security"])

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
        self.assertEqual(self.c.get("/api/photos").status_code, 401)
        self.login()
        self.store.set_password_hash("owner", hash_password("another long password"))
        self.assertEqual(self.c.get("/api/photos").status_code, 401)

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

    def test_review_process_and_approve_through_the_worker(self):
        headers = self.csrf()
        self.assertEqual(self.c.post("/api/review/items/1/process", headers=headers).json()["status"], "processing")
        self.wait()
        item = self.c.get("/api/review/items/1").json()
        self.assertEqual(item["status"], "pending")
        choices = [{"approve": c["new"] == "Food"} if c["action"] == "add_to_album" else {"approve": False}
                   for c in item["changes"]]
        r = self.c.post("/api/review/items/1/decide", json={"choices": choices}, headers=headers)
        self.assertEqual(r.json()["status"], "applying")
        self.wait()
        self.assertIn("1", self.fl.sets["102"]["photos"])                    # Food, on (fake) Flickr
        self.assertEqual(self.c.get("/api/review/items/1").json()["status"], "applied")

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
        overview = self.c.get("/api/analytics/overview", params={"days": 3650}).json()
        self.assertEqual(overview["top"][0]["photo_id"], "1")
        self.assertEqual(self.c.get("/api/analytics/breakdown", params={"by": "year", "days": 3650}).json()[0]["views"], 40)
        self.assertEqual(self.c.get("/api/analytics/photos/1").json()["daily"][0]["views"], 40)
        self.assertEqual(self.c.get("/api/analytics/breakdown", params={"by": "secrets"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
