"""Unexpected errors reported to GitHub as issues (errors.py): one issue per kind of error, a comment for a
repeat, nothing for the network, nothing at all without a token, and no secrets in what's sent."""
import os
import tempfile
import time
import unittest

import requests
from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from coconut_barometer import errors
from coconut_barometer.config import from_dict
from coconut_barometer.web.app import create_app


class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self._data, self.content = status, data, b"x"

    def json(self):
        return self._data


class FakeGitHub:
    """The issues endpoints, in memory."""
    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.comments: list[tuple[int, str]] = []

    def request(self, method, url, json=None, **kw):
        path = url.split("/repos/owner/repo", 1)[1]
        if method == "POST" and path == "/issues":
            n = len(self.issues) + 1
            self.issues[n] = {"number": n, "state": "open", **json}
            return FakeResponse(201, self.issues[n])
        if method == "POST" and path.endswith("/comments"):
            self.comments.append((int(path.split("/")[2]), json["body"]))
            return FakeResponse(201, {})
        if method == "GET" and path.startswith("/issues?"):
            return FakeResponse(200, [i for i in self.issues.values() if i["state"] == "open"])
        if method == "GET":
            return FakeResponse(200, self.issues[int(path.split("/")[2])])
        raise AssertionError(f"unexpected {method} {url}")


def boom(message="it broke"):
    raise ValueError(message)


def caught(fn, *a):
    try:
        fn(*a)
    except Exception as e:
        return e


class ReporterTest(unittest.TestCase):
    def setUp(self):
        self.r = errors.Reporter()
        self.gh = self.r.session = FakeGitHub()
        self.r._start = lambda: None          # sent by send_all, in the test's thread
        self.r.watch({"github": {"token": "ghp_secret_token_value", "repo": "owner/repo"},
                      "vision": {"api_key": "sk-ant-very-secret"}})

    def send_all(self):
        while not self.r._q.empty():
            self.r._send(*self.r._q.get())

    def test_one_issue_per_kind_and_a_comment_for_a_repeat(self):
        self.r.report(caught(boom), "sync job")
        self.send_all()
        self.assertEqual(len(self.gh.issues), 1)
        issue = self.gh.issues[1]
        self.assertEqual(issue["title"], "ValueError: it broke")
        self.assertIn("`sync job`", issue["body"])
        self.assertEqual(issue["labels"], [errors.LABEL])

        self.r.report(caught(boom, "different words, same place"), "sync job")
        self.send_all()
        self.assertEqual(len(self.gh.issues), 1, "not before COMMENT_EVERY")
        self.r._last_comment.clear()                     # an hour later
        self.r.report(caught(boom), "sync job")
        self.send_all()
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(self.gh.comments[0][0], 1)
        self.assertIn("2 times since the last comment", self.gh.comments[0][1])

    def test_a_closed_issue_opens_a_new_one(self):
        self.r.report(caught(boom), "x")
        self.send_all()
        self.gh.issues[1]["state"] = "closed"
        self.r._last_comment.clear()
        self.r.report(caught(boom), "x")
        self.send_all()
        self.assertEqual(len(self.gh.issues), 2)

    def test_an_issue_from_before_a_restart_is_found(self):
        self.r.report(caught(boom), "x")
        self.send_all()
        fresh = errors.Reporter()
        fresh.session, fresh._start = self.gh, lambda: None
        fresh.watch(self.r.cfg)
        fresh.report(caught(boom), "x")
        while not fresh._q.empty():
            fresh._send(*fresh._q.get())
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(len(self.gh.comments), 1)

    def test_the_network_and_no_token_send_nothing(self):
        self.r.report(caught(lambda: (_ for _ in ()).throw(requests.ConnectionError("down"))), "x")
        self.r.report(caught(lambda: (_ for _ in ()).throw(TimeoutError())), "x")
        self.assertTrue(self.r._q.empty())
        self.r.cfg["github"]["token"] = None
        self.r.report(caught(boom), "x")
        self.assertTrue(self.r._q.empty())

    def test_secrets_are_scrubbed(self):
        self.r.report(caught(boom, "GET https://api.example/x?api_key=abc123&q=1 with sk-ant-very-secret"), "x")
        self.send_all()
        body = self.gh.issues[1]["body"] + self.gh.issues[1]["title"]
        self.assertNotIn("abc123", body)
        self.assertNotIn("sk-ant-very-secret", body)
        self.assertIn("api_key=[redacted]", body)

    def test_no_more_than_new_per_hour(self):
        self.r._opened = [time.time()] * errors.NEW_PER_HOUR
        self.r.report(caught(boom), "x")
        with self.assertRaises(RuntimeError):
            self.send_all()
        self.assertEqual(self.gh.issues, {})


class CaughtTest(unittest.TestCase):
    def setUp(self):
        self.reported = []
        original = errors.reporter.report
        errors.reporter.report = lambda exc, where: self.reported.append(where) if not errors.expected(exc) else None
        self.addCleanup(setattr, errors.reporter, "report", original)

    def test_a_bug_is_an_error_with_its_traceback_and_reported(self):
        with self.assertLogs("coconut_barometer.errors") as log:
            errors.caught(caught(boom), "sync job")
        self.assertEqual(log.records[0].levelname, "ERROR")
        self.assertIn("Traceback", log.output[0])
        self.assertEqual(self.reported, ["sync job"])

    def test_the_outside_world_is_a_warning_and_not_reported(self):
        class APIError(Exception):
            pass
        APIError.__module__ = "anthropic._exceptions"
        with self.assertLogs("coconut_barometer.errors") as log:
            errors.caught(caught(lambda: (_ for _ in ()).throw(requests.ConnectionError("down"))), "aprs")
            errors.caught(caught(lambda: (_ for _ in ()).throw(APIError("overloaded"))), "vision")
        self.assertEqual([r.levelname for r in log.records], ["WARNING", "WARNING"])
        self.assertNotIn("Traceback", "".join(log.output))
        self.assertEqual(self.reported, [])


class ServerErrorTest(unittest.TestCase):
    def test_a_500_is_reported(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]}})
        app = create_app(cfg, FakeFlickr(), provider_factory=lambda: FakeVision("m"), scheduler=False)

        @app.get("/api/explode")
        def explode():
            boom()
        app.router.routes.insert(0, app.router.routes.pop())    # ahead of the catch-all 404

        seen = []
        original = errors.reporter.report
        errors.reporter.report = lambda exc, where: seen.append((type(exc), where))
        self.addCleanup(setattr, errors.reporter, "report", original)
        r = TestClient(app, base_url="https://testserver", raise_server_exceptions=False).get("/api/explode")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(seen, [(ValueError, "GET /api/explode")])


if __name__ == "__main__":
    unittest.main()
