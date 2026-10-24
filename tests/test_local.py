"""Local mode (local.py, #92): serving on a laptop without acting as the station. Chosen by --local/--production,
COCONUT_LOCAL, else by listening on 127.0.0.1 off Render; then no APRS-IS or CWOP, no push, no GitHub reports, no
Flickr sync or backups, while the read-only services still start, and the Status page shows them off."""
import os
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from test_alerts import SUB
from test_errors import FakeGitHub, boom, caught
from coconut_barometer import errors, local, push
from coconut_barometer.config import from_dict
from coconut_barometer.store import Store
from coconut_barometer.web.api import status as status_api
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import hash_password


class LocalModeTest(unittest.TestCase):
    def setUp(self):
        local.set_on(True)
        self.addCleanup(local.set_on, False)

    def test_who_decides(self):
        self.assertTrue(local.decide("127.0.0.1", environ={}))                       # a laptop: local
        self.assertFalse(local.decide("127.0.0.1", environ={"RENDER": "true"}))      # Render: live
        self.assertFalse(local.decide("0.0.0.0", environ={}))                        # reachable from outside: live
        self.assertFalse(local.decide("127.0.0.1", False, environ={}))               # --production, behind Caddy
        self.assertTrue(local.decide("0.0.0.0", True, environ={"RENDER": "true"}))   # --local wins over all
        self.assertFalse(local.decide("127.0.0.1", environ={"COCONUT_LOCAL": "0"}))
        self.assertTrue(local.decide("0.0.0.0", environ={"COCONUT_LOCAL": "yes"}))
        self.assertTrue(local.decide("localhost", environ={"COCONUT_LOCAL": "maybe"}))   # not a yes or no: by host

    def test_nothing_is_sent_as_the_station_but_the_reading_still_runs(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]},
                         "aprs": {"callsigns": ["KK6GPV-9"], "passcode": "12345", "cwop": {"Conroe": "KK6GPV-13"}}})
        app = create_app(cfg, FakeFlickr(), scheduler=False)
        rt = app.state.runtime
        with mock.patch.object(rt.radar, "start") as radar, mock.patch.object(rt.weather, "start_stream") as weather:
            rt.start_scheduler()
        self.addCleanup(rt.stop)
        self.assertEqual((rt.aprs._stream, rt.aprs._reporter), (None, None))       # no APRS-IS login, no CWOP
        self.assertEqual(rt._scheduler.name, "scheduler (local mode)")              # no Flickr sync, no backups
        self.assertIsNone(rt.next_sync)
        self.assertEqual((radar.call_count, weather.call_count), (1, 1))            # what's only read: on

    def test_push_and_github_reports_are_only_logged(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))
        push.subscribe(store, SUB, "owner", "Phone", "https://example.com")
        with mock.patch("pywebpush.webpush") as webpush, self.assertLogs("coconut_barometer.local", "INFO") as log:
            self.assertEqual(push.send(store, "Storm coming", "…"), {"sent": 0, "failed": [], "removed": 0, "local": True})
        self.assertEqual(webpush.call_count, 0)
        self.assertIn("not sending a push notification: Storm coming", log.output[0])
        r = errors.Reporter()
        r.session, r._start = FakeGitHub(), lambda: None
        r.watch({"github": {"token": "ghp_x", "repo": "owner/repo"}})
        r.report(caught(boom), "sync job")
        self.assertTrue(r._q.empty())                                                # not even queued

    def test_the_status_page_shows_what_is_held_back_as_off(self):
        status_api._cache.clear()
        self.addCleanup(status_api._cache.clear)
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        Store(path).set_password_hash("owner", hash_password("correct horse battery"))
        cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]}, "aprs": {"callsigns": ["KK6GPV-9"]},
                         "github": {"token": "ghp_x", "repo": "owner/repo"}})
        c = TestClient(create_app(cfg, FakeFlickr(), provider_factory=lambda: FakeVision("m"), scheduler=False), base_url="https://testserver")
        c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"})
        d = c.get("/api/status").json()
        rows = {r["id"]: r for r in d["rows"]}
        for k in ("aprs", "cwop", "flickr", "backups", "errors"):
            self.assertEqual((rows[k]["level"], rows[k]["state"]), ("off", local.STATE), k)
        self.assertTrue(d["local"])
        status_api._cache.clear()
        local.set_on(False)
        d = c.get("/api/status").json()
        self.assertEqual((d["local"], {r["id"]: r for r in d["rows"]}["aprs"]["state"] != local.STATE), (False, True))


class ServeTest(unittest.TestCase):
    """`coconut-barometer serve`: the flags reach the app, and local mode has no Flickr client to sync or apply with."""

    def serve(self, *flags, host="127.0.0.1", environ=None):
        from argparse import Namespace
        from coconut_barometer import cli

        self.addCleanup(local.set_on, False)
        cfg = from_dict({**CFG, "database": os.path.join(tempfile.mkdtemp(), "db.duckdb"), "web": {"allowed_hosts": ["testserver"]}})
        flag = True if "--local" in flags else False if "--production" in flags else None
        seen = {}
        def run(app, **kw):
            seen["client"] = app.state.runtime.client
            seen["factory"] = app.state.runtime.client_factory(cfg)
        with mock.patch("uvicorn.run", run), mock.patch.object(cli, "_new_client", lambda cfg: "flickr"), \
                mock.patch.dict(os.environ, environ or {}, clear=False), mock.patch.object(cli, "_restore_on_start", lambda cfg, s: s):
            os.environ.pop("RENDER", None) if not (environ or {}).get("RENDER") else None
            cli.cmd_serve(cfg, None, Namespace(host=host, port=8000, config=None, local=flag))
        return local.on(), seen

    def test_flags_and_the_default(self):
        self.assertEqual(self.serve(), (True, {"client": None, "factory": None}))             # a laptop
        self.assertEqual(self.serve("--production"), (False, {"client": "flickr", "factory": "flickr"}))
        self.assertEqual(self.serve(environ={"RENDER": "true"})[0], False)
        self.assertEqual(self.serve("--local", environ={"RENDER": "true"})[0], True)

    def test_the_command_line(self):
        import argparse

        from coconut_barometer import cli

        parse, got = argparse.ArgumentParser.parse_args, []
        class Parsed(Exception):
            pass
        def capture(self, *a, **k):
            got.append(parse(self, *a, **k))
            raise Parsed
        with mock.patch.object(argparse.ArgumentParser, "parse_args", capture):
            for argv in (["serve"], ["serve", "--local"], ["serve", "--production"]):
                with self.assertRaises(Parsed):
                    cli.main(argv)
        self.assertEqual([a.local for a in got], [None, True, False])
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):                     # not both
            cli.main(["serve", "--local", "--production"])


if __name__ == "__main__":
    unittest.main()
