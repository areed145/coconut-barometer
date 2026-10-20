"""The real server under parallel requests, as a browser sends them (the analytics page
fires several at once). FastAPI runs a request's dependencies and handler on different
pool threads, which once broke sqlite's same-thread rule."""
import os
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import httpx
import uvicorn

from fakes import FakeFlickr, CFG
from flickr_organizer.config import from_dict
from flickr_organizer.store import Store
from flickr_organizer.sync import sync
from flickr_organizer.web.app import create_app
from flickr_organizer.web.security import hash_password


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), "db.sqlite")
        store = Store(path)
        sync(FakeFlickr(), store, log=lambda *a: None)
        store.set_password_hash("owner", hash_password("correct horse battery"))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        cfg = from_dict({**CFG, "database": path,
                         "web": {"allowed_hosts": ["127.0.0.1"], "secure_cookies": False, "sync_every_minutes": 0}})
        app = create_app(cfg, FakeFlickr(), scheduler=False)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        threading.Thread(target=self.server.run, daemon=True).start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        self.addCleanup(setattr, self.server, "should_exit", True)

    def test_parallel_requests_all_succeed(self):
        base = f"http://127.0.0.1:{self.port}"
        with httpx.Client(base_url=base) as c:
            c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"}).raise_for_status()
            paths = ["/api/photos", "/api/albums", "/api/review/summary", "/api/analytics/overview?days=3650",
                     "/api/analytics/gainers?days=90", "/api/analytics/breakdown?by=album", "/api/sync/status",
                     "/api/photos/1"] * 5
            with ThreadPoolExecutor(8) as pool:
                codes = list(pool.map(lambda p: (p, c.get(p).status_code), paths))
        self.assertEqual([x for x in codes if x[1] != 200], [])


if __name__ == "__main__":
    unittest.main()
