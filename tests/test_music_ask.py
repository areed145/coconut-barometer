"""Questions about your listening (music_ask.py, #204 step 2): the tools over the whole graph, the loop with Claude
(scripted here), and that only real artists and links reach the page."""
import datetime as dt
import json
import os
import tempfile
import unittest
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from coconut_barometer import music_ask as Q
from coconut_barometer import music_explore as X
from coconut_barometer import music_graph as G
from coconut_barometer.config import from_dict
from coconut_barometer.store import Store
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import hash_password
from test_music_explore import NOW, SAID, rows

ON = {"vision": {"provider": "claude", "api_key": "k", "model": "claude-test"}}
Y2025 = int(dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc).timestamp())


def whole():
    """The explorer's test library, with Coltrane first played in 2025 and the rest long before."""
    r = rows()
    for x in r:
        if x["artist"] == "Coltrane":
            x["played_at"] += Y2025
    r.sort(key=lambda x: x["played_at"])
    known = {x["artist"] for x in r}
    plays = [(G.credited(x["artist"], "", known)[0], x["played_at"]) for x in r]
    return X.Whole(G.build(r, limit=None, profiles=SAID, now=NOW, min_plays=10), NOW, 0.1, plays)


def tool(name, args, i=1):
    return NS(type="tool_use", id=f"toolu_{i}", name=name, input=args)


def reply(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop, usage=NS(input_tokens=100, output_tokens=20))


class Script:
    """Claude's replies in order, and what each request carried."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def __call__(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        return self.replies.pop(0)


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.t = Q.Tools(whole())

    def test_find_artists(self):
        got = self.t.run("find_artists", {"scene": "bakersfield sound"})
        self.assertEqual([a["artist"] for a in got["artists"]], ["Haggard", "Owens"])
        self.assertEqual(self.t.run("find_artists", {"name": "wil"})["artists"][0]["artist"], "Wilco")

    def test_artist_info_says_when(self):
        got = self.t.run("artist_info", {"artist": "coltrane"})
        self.assertEqual(got["artist"], "Coltrane")
        self.assertEqual(list(got["plays_by_year"]), [2025])
        self.assertEqual(got["first_played"][:4], "2025")

    def test_connect_and_neighbourhood_give_reasons(self):
        got = self.t.run("connect", {"a": "Haggard", "b": "Parsons"})
        self.assertEqual(got["path"], ["Haggard", "Owens", "Parsons"])
        self.assertRegex(got["steps"][0]["why"][0], r"featured on|together")
        self.assertIn("links", self.t.run("neighbourhood", {"artist": "Owens"}))

    def test_listening_finds_what_was_new(self):
        got = self.t.run("listening", {"group_by": "scene", "since": "2025-01-01", "new_only": True})
        self.assertEqual([r["value"] for r in got["rows"]], ["hard bop"])
        everything = self.t.run("listening", {"group_by": "artist"})
        self.assertEqual(everything["rows"][0]["value"], "Haggard")

    def test_bad_input_is_an_error_not_a_crash(self):
        with self.assertRaises(X.NotFound):
            self.t.run("listening", {"group_by": "scene", "since": "last year"})
        with self.assertRaises(X.NotFound):
            self.t.run("artist_info", {"artist": "Nobody"})


class AskTest(unittest.TestCase):
    def setUp(self):
        self.w = whole()

    def test_tools_then_an_answer_with_only_real_links(self):
        s = Script(
            reply(NS(type="text", text="Looking."), tool("connect", {"a": "Haggard", "b": "Parsons"}, 1)),
            reply(tool("show_answer", {"text": "Owens links them.", "artists": ["Haggard", "Ghost"],
                                       "links": [["Haggard", "Owens"], ["Owens", "Parsons"], ["Haggard", "Coltrane"]]}, 2), stop="tool_use"))
        got = Q.ask(ON, self.w, "How does Haggard reach Parsons?", today=dt.date(2026, 11, 7), create=s)
        self.assertEqual(got["text"], "Owens links them.")
        self.assertEqual([(e["a"], e["b"]) for e in got["links"]], [("Haggard", "Owens"), ("Owens", "Parsons")])   # not invented
        self.assertEqual(got["artists"], ["Haggard", "Owens", "Parsons"])                                       # no Ghost
        self.assertEqual((got["input_tokens"], got["output_tokens"]), (200, 40))
        second = s.requests[1]["messages"]
        self.assertEqual(second[1]["role"], "assistant")                                  # the reply, unchanged
        result = second[2]["content"][0]
        self.assertEqual((result["type"], result["tool_use_id"], result["is_error"]), ("tool_result", "toolu_1", False))
        self.assertEqual(json.loads(result["content"])["path"], ["Haggard", "Owens", "Parsons"])
        self.assertIn("2026-11-07", s.requests[0]["system"])
        self.assertEqual(s.requests[0]["model"], "claude-test")

    def test_a_tool_error_goes_back_to_claude(self):
        s = Script(reply(tool("artist_info", {"artist": "Nobody"})),
                   reply(tool("show_answer", {"text": "Not in your library.", "artists": [], "links": []}, 2)))
        self.assertEqual(Q.ask(ON, self.w, "Who is Nobody?", create=s)["text"], "Not in your library.")
        self.assertTrue(s.requests[1]["messages"][2]["content"][0]["is_error"])

    def test_a_plain_text_answer_is_shown_without_highlights(self):
        s = Script(reply(NS(type="text", text="You mostly play country."), stop="end_turn"))
        got = Q.ask(ON, self.w, "What do I play?", create=s)
        self.assertEqual((got["text"], got["artists"]), ("You mostly play country.", []))

    def test_refusals_long_answers_and_endless_loops_say_so(self):
        for r, words in ((reply(stop="refusal"), "declined"), (reply(stop="max_tokens"), "too long")):
            with self.assertRaises(Q.AskError) as e:
                Q.ask(ON, self.w, "q", create=Script(r))
            self.assertIn(words, str(e.exception))
        endless = Script(*[reply(tool("find_artists", {}, i)) for i in range(Q.MAX_TURNS)])
        with self.assertRaises(Q.AskError):
            Q.ask(ON, self.w, "q", create=endless)

    def test_needs_a_question_and_a_key(self):
        with self.assertRaises(Q.AskError):
            Q.ask(ON, self.w, "  ")
        with self.assertRaises(Q.AskError):
            Q.ask({"vision": {"provider": "ollama", "api_key": "k", "model": "m"}}, self.w, "anything")


class ApiTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        c = from_dict({**CFG, "database": os.path.join(d, "t.duckdb"), "web": {"secure_cookies": False, "allowed_hosts": ["testserver"]}})
        store = Store(c["database"])
        store.set_password_hash("owner", hash_password("correct horse battery"))
        store.close()
        self.app = create_app(c, FakeFlickr(), lambda: FakeVision(), background=False, scheduler=False)
        rt = self.app.state.runtime
        rt.explorer._whole, rt.explorer._key = whole(), rt.store().music_graph_version()
        self.c = TestClient(self.app, base_url="http://testserver")
        self.csrf = self.c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"}).json()["csrf"]

    def post(self, body):
        return self.c.post("/api/music/explore/ask", json=body, headers={"X-CSRF-Token": self.csrf})

    def test_the_answer_comes_with_its_piece_of_the_graph(self):
        s = Script(reply(tool("show_answer", {"text": "Close.", "artists": ["Owens"], "links": [["Owens", "Parsons"]]})))
        real, Q._client = Q._client, lambda cfg: NS(messages=NS(create=s))
        self.addCleanup(setattr, Q, "_client", real)
        self.app.state.runtime.cfg["vision"].update(ON["vision"])
        self.assertTrue(self.c.get("/api/music/explore/facets").json()["can_ask"])
        got = self.post({"question": "Who's near Owens?", "have": ["Haggard"]}).json()
        self.assertEqual({n["id"] for n in got["nodes"]}, {"Owens", "Parsons"})
        self.assertIn(("Haggard", "Owens"), [(e["a"], e["b"]) for e in got["edges"]])      # joins what's shown
        self.assertEqual(got["text"], "Close.")

    def test_without_a_key_it_says_so(self):
        self.app.state.runtime.cfg["vision"]["api_key"] = None
        self.assertEqual(self.post({"question": "Anything?"}).status_code, 409)
        self.assertFalse(self.c.get("/api/music/explore/facets").json()["can_ask"])


if __name__ == "__main__":
    unittest.main()
