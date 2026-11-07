"""Artist profiles from Claude (artist_profiles.py): who's asked, what's kept, the daily limit, and the jobs."""
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from coconut_barometer import artist_profiles as AP
from coconut_barometer import local
from coconut_barometer.config import from_dict
from coconut_barometer.store import Store
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import hash_password

PASSWORD = "correct horse battery"
NOON = 1_800_000_000
ON = {"artist_profiles": {"enabled": True, "daily_calls": 2},
      "vision": {"provider": "claude", "api_key": "k", "model": "claude-test"}}


def fill(store, plays: dict):
    for artist, n in plays.items():
        store.db.execute("INSERT INTO music_tracks (id, kind, name, artist, album, genres) VALUES (?, 'songs', 'x', ?, '', '[]')", (artist, artist))
        for i in range(n):
            store.db.execute("INSERT INTO music_plays (track_id, played_at, seen_at, source) VALUES (?, ?, ?, 'export')", (artist, i, i))


class Cleaning(unittest.TestCase):
    def test_what_is_kept(self):
        got = AP.clean({"known": True, "scenes": ["Bakersfield  Sound", "bakersfield sound", "", "a", "b", "c"],
                        "styles": ["Country Rock"], "decades": [1960, 1965, 1970.0, 2090, "1980"], "country": "us"}, 2026)
        self.assertEqual(got, {"scenes": ["bakersfield sound", "a", "b"], "styles": ["country rock"], "decades": [1960, 1970],
                               "country": "US"})

    def test_unknown_is_empty_and_a_bad_country_is_none(self):
        self.assertEqual(AP.clean({"known": False, "scenes": ["x"]}, 2026), {"scenes": [], "styles": [], "decades": [], "country": None})
        self.assertIsNone(AP.clean({"known": True, "country": "USA"}, 2026)["country"])

    def test_needs_an_anthropic_key(self):
        self.assertTrue(AP.can_ask(ON))
        self.assertFalse(AP.can_ask({"vision": {"provider": "ollama", "api_key": "k"}}))
        self.assertFalse(AP.ArtistProfiles({**ON, "artist_profiles": {"enabled": False}}).ready())


class Jobs(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
        self.addCleanup(self.store.close)
        fill(self.store, {"Band": 9, "Band feat. Guest": 3, "Solo": 5, "Quiet": 1})
        self.prompts = []

    def profiles(self, answer=None, cfg=ON, batch=None):
        def ask(prompt):
            self.prompts.append(prompt)
            return (answer if answer is not None else {"artists": [
                {"name": "Band", "known": True, "scenes": ["Britpop"], "styles": ["indie rock"], "decades": [1990], "country": "GB"},
                {"name": "Ghost", "known": True, "scenes": ["made up"]}]}), 100, 50
        return AP.ArtistProfiles(cfg, ask=ask)

    def test_the_top_artists_count_their_features(self):
        self.assertEqual(AP.top_artists(self.store), ["Band", "Solo", "Quiet"])

    def test_a_job_asks_once_and_keeps_only_listed_names(self):
        p = self.profiles()
        out = p.run_once(self.store, NOON)
        self.assertEqual((out["status"], out["rows"], out["input_tokens"]), ("done", 3, 100))
        have = self.store.profiles()
        self.assertEqual(set(have), {"Band", "Solo", "Quiet"})                   # Ghost wasn't asked about
        self.assertEqual(have["Band"]["scenes"], ["britpop"])
        self.assertEqual(have["Solo"], {"scenes": [], "styles": [], "decades": [], "country": None})   # left out: not asked again
        self.assertEqual(p.to_ask(self.store), [])
        self.assertEqual(p.run_once(self.store, NOON)["rows"], 0)
        self.assertEqual(len(self.prompts), 1)

    def test_the_labels_in_use_are_offered_back(self):
        self.store.profiles_save([{"artist": "Solo", "scenes": ["britpop"], "styles": ["shoegaze"], "decades": [], "country": None}], "m", 1)
        self.profiles().run_once(self.store, NOON)
        self.assertIn("Scenes in use: britpop", self.prompts[0])
        self.assertIn("shoegaze", self.prompts[0])
        self.assertNotIn("- Solo", self.prompts[0])

    def test_the_daily_limit(self):
        p = self.profiles(answer={"artists": []})
        AP.BATCH, batch = 1, AP.BATCH
        self.addCleanup(setattr, AP, "BATCH", batch)
        self.assertEqual(p.run_once(self.store, NOON)["status"], "done")
        self.assertEqual(p.run_once(self.store, NOON + 60)["status"], "done")
        self.assertEqual(p.run_once(self.store, NOON + 120)["status"], "skipped")      # 2 a day
        self.assertFalse(p.due(self.store, NOON + 120))
        self.assertEqual(p.run_once(self.store, NOON + 86400)["status"], "done")      # a new day
        self.assertEqual(len(self.prompts), 3)

    def test_off_asks_nothing(self):
        p = self.profiles(cfg={**ON, "artist_profiles": {"enabled": False, "daily_calls": 5}})
        self.assertEqual(p.run_once(self.store, NOON)["status"], "skipped")
        self.assertEqual(self.prompts, [])

    def test_the_state(self):
        self.profiles().run_once(self.store, NOON)
        s = self.profiles().state(self.store)
        self.assertEqual((s["top"], s["asked"], s["daily_calls"]), (3, 3, 2))


class Api(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        c = from_dict({**CFG, "database": os.path.join(self.dir, "t.duckdb"), "web": {"secure_cookies": False, "allowed_hosts": ["testserver"]}})
        store = Store(c["database"])
        store.set_password_hash("owner", hash_password(PASSWORD))
        store.close()
        self.app = create_app(c, FakeFlickr(), lambda: FakeVision(), background=False, scheduler=False)
        self.rt = self.app.state.runtime
        self.c = TestClient(self.app, base_url="http://testserver")
        self.csrf = self.c.post("/api/auth/login", json={"username": "owner", "password": PASSWORD}).json()["csrf"]

    def send(self, method="post"):
        return getattr(self.c, method)("/api/music/graph/profiles", headers={"X-CSRF-Token": self.csrf})

    def test_profiles_go_to_the_back_of_the_line_one_at_a_time(self):
        self.assertEqual(self.send().json(), {"status": "not_ready"})
        self.rt.cfg["artist_profiles"].update(ON["artist_profiles"])
        self.rt.cfg["vision"].update(ON["vision"])
        self.rt.review.jobs.put(("sync", ""))
        self.assertEqual(self.send().json(), {"status": "queued"})
        self.assertEqual(self.send().json(), {"status": "busy"})
        self.assertEqual(list(self.rt.review.jobs.queue), [("sync", ""), ("profiles", "")])

    def test_starting_over_forgets_them(self):
        self.rt.store().profiles_save([{"artist": "A", "scenes": [], "styles": [], "decades": [], "country": None}], "m", 1)
        self.assertEqual(self.send("delete").json(), {"status": "cleared"})
        self.assertEqual(self.rt.store().profiles(), {})

    def test_held_in_local_mode(self):
        local.set_on(True)
        self.addCleanup(local.set_on, False)
        self.assertEqual(self.send().status_code, 409)

    def test_the_graph_says_how_far_they_are_and_the_old_button_is_gone(self):
        g = self.c.get("/api/music/graph", params={"limit": 50}).json()
        self.assertEqual((g["profiles"]["enabled"], g["profiles"]["top"]), (False, 0))
        self.assertNotIn("can_ask", g)
        self.assertEqual(self.c.post("/api/music/graph/ask", headers={"X-CSRF-Token": self.csrf}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
