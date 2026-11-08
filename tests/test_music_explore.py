"""The music explorer (music_explore.py, #204): the whole graph, kept until its data changes, and its queries."""
import json
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from coconut_barometer import music_explore as X
from coconut_barometer import music_graph as G
from coconut_barometer.config import from_dict
from coconut_barometer.store import Store
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import hash_password

PASSWORD = "correct horse battery"
NOW = 1_800_000_000
# Who plays with whom ("A (feat. B)" titles), and how much each is played: Quiet has too few plays to explore.
PLAYS = {"Haggard": 40, "Owens": 30, "Parsons": 25, "Wilco": 20, "Tweedy": 15, "Coltrane": 12, "Quiet": 5}
FEATS = [("Haggard", "Owens"), ("Owens", "Parsons"), ("Parsons", "Wilco"), ("Wilco", "Tweedy")]
SAID = {"Haggard": {"scenes": ["bakersfield sound"], "styles": ["honky tonk"], "decades": [1960, 1970], "country": "US"},
        "Owens": {"scenes": ["bakersfield sound"], "styles": ["honky tonk"], "decades": [1960], "country": "US"},
        "Coltrane": {"scenes": ["hard bop"], "styles": ["jazz"], "decades": [1950, 1960], "country": "US"}}


def rows():
    out, t = [], 0
    for artist, n in PLAYS.items():
        for _ in range(n):
            out.append({"played_at": t, "ms": 180000, "artist": artist, "name": "Song", "composer": "", "genres": json.dumps(["Country"])})
            t += 1
    for a, b in FEATS:
        out.append({"played_at": t, "ms": 180000, "artist": a, "name": f"Duet (feat. {b})", "composer": "", "genres": "[]"})
        t += 1
    return out


def whole():
    return X.Whole(G.build(rows(), limit=None, profiles=SAID, now=NOW, min_plays=10), NOW, 0.1)


class Queries(unittest.TestCase):
    def setUp(self):
        self.w = whole()

    def test_the_whole_graph_is_every_artist_over_ten_plays(self):
        self.assertEqual(set(self.w.nodes), set(PLAYS) - {"Quiet"})
        self.assertEqual(self.w.nodes["Haggard"]["profile"]["era"], "1960s–70s")

    def test_a_neighbourhood_grows_by_the_strongest_links(self):
        one = self.w.neighbourhood("owens")                                     # any case
        self.assertEqual(one["focus"], "Owens")
        self.assertEqual({n["id"] for n in one["nodes"]}, {"Owens", "Haggard", "Parsons"})
        two = self.w.neighbourhood("Owens", depth=2)
        self.assertIn("Wilco", {n["id"] for n in two["nodes"]})
        self.assertEqual(len(self.w.neighbourhood("Owens", depth=2, size=2)["nodes"]), 2)

    def test_a_piece_brings_its_links_to_what_the_page_has(self):
        p = self.w.piece(["Parsons"], have=["Owens", "Nobody"])
        self.assertEqual([(e["a"], e["b"]) for e in p["edges"]], [("Owens", "Parsons")])
        self.assertEqual([n["id"] for n in p["nodes"]], ["Parsons"])
        both = self.w.piece(["Owens", "Parsons"])
        self.assertEqual(len(both["edges"]), 1)                                # each link once

    def test_filters(self):
        self.assertEqual([n["id"] for n in self.w.filter(scene="Bakersfield Sound")["nodes"]], ["Haggard", "Owens"])
        self.assertEqual(self.w.matches(decade=1950), ["Coltrane"])
        self.assertEqual(self.w.matches(decade=1960, genre="jazz"), ["Coltrane"])
        self.assertEqual(self.w.filter(country="us", size=1)["matched"], 3)

    def test_a_path_follows_links_and_says_why(self):
        p = self.w.path("Haggard", "Tweedy")
        self.assertEqual(p["path"], ["Haggard", "Owens", "Parsons", "Wilco", "Tweedy"])
        self.assertEqual(len(p["steps"]), 4)
        self.assertEqual(p["steps"][0]["rels"][0]["type"], "featured")
        with self.assertRaises(X.NotFound):
            self.w.path("Haggard", "Coltrane")                                # nothing links them
        with self.assertRaises(X.NotFound):
            self.w.path("Haggard", "Quiet")                                   # too few plays

    def test_facets(self):
        f = self.w.facets()
        self.assertEqual(f["artists"][0], {"id": "Haggard", "plays": 41})
        self.assertIn(["bakersfield sound", 2], [list(s) for s in f["scenes"]])
        self.assertIn([1950, 1], [list(d) for d in f["decades"]])
        self.assertEqual(f["min_plays"], 10)


class Keeping(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
        self.addCleanup(self.store.close)
        for i, r in enumerate(rows()):
            tid = f"t{i}"
            self.store.db.execute("INSERT INTO music_tracks (id, kind, name, artist, album, genres, composer) VALUES (?, 'songs', ?, ?, '', ?, '')",
                                  (tid, r["name"], r["artist"], r["genres"]))
            self.store.db.execute("INSERT INTO music_plays (track_id, played_at, seen_at, source) VALUES (?, ?, ?, 'export')", (tid, r["played_at"], r["played_at"]))

    def change(self, scene="outlaw country"):
        self.store.profiles_save([{"artist": "Haggard", "scenes": [scene], "styles": [], "decades": [], "country": None}], "m", NOW)

    def test_built_once_and_again_when_its_data_changes(self):
        now = [NOW]
        ex = X.Explorer(clock=lambda: now[0])
        first = ex.whole(self.store)
        self.assertIs(ex.whole(self.store), first)
        self.change()
        self.assertIs(ex.whole(self.store), first)                  # changed, but built moments ago (#207)
        now[0] += X.REBUILD_EVERY
        again = ex.whole(self.store)
        self.assertIsNot(again, first)
        self.assertEqual(again.nodes["Haggard"]["profile"]["scenes"], ["outlaw country"])
        self.assertIs(ex.whole(self.store), again)                  # and nothing changed since

    def test_a_rebuild_happens_in_the_background_the_old_graph_answering_meanwhile(self):
        import threading

        now, opened, release = [NOW], [], threading.Event()
        path = self.store.path

        def open_store():
            release.wait(5)                                         # the request has had its answer by now
            s = Store(path, migrate=False)
            opened.append(s)
            return s

        ex = X.Explorer(clock=lambda: now[0], open_store=open_store)
        first = ex.whole(self.store)                                # the first build is in the request
        self.change()
        now[0] += X.REBUILD_EVERY
        self.assertIs(ex.whole(self.store), first)
        self.assertIn("rebuilding", ex.describe())
        self.assertIs(ex.whole(self.store), first)                  # one rebuild at a time
        release.set()
        for t in [t for t in threading.enumerate() if t.name == "music-explorer"]:
            t.join(10)
        self.assertEqual(len(opened), 1)
        again = ex.whole(self.store)
        self.assertIsNot(again, first)
        self.assertEqual(again.nodes["Haggard"]["profile"]["scenes"], ["outlaw country"])
        self.assertNotIn("rebuilding", ex.describe())

    def test_a_failed_background_rebuild_keeps_the_graph_and_waits_to_try_again(self):
        import threading

        now, tries = [NOW], []

        def open_store():
            tries.append(1)
            raise OSError("no")

        ex = X.Explorer(clock=lambda: now[0], open_store=open_store)
        first = ex.whole(self.store)
        self.change()
        now[0] += X.REBUILD_EVERY
        with self.assertLogs("coconut_barometer.music_explore", "ERROR"):
            self.assertIs(ex.whole(self.store), first)
            for t in [t for t in threading.enumerate() if t.name == "music-explorer"]:
                t.join(10)
        self.assertIs(ex.whole(self.store), first)
        self.assertEqual(len(tries), 1)                             # not again until REBUILD_EVERY has passed


class Api(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        c = from_dict({**CFG, "database": os.path.join(self.dir, "t.duckdb"), "web": {"secure_cookies": False, "allowed_hosts": ["testserver"]}})
        store = Store(c["database"])
        store.set_password_hash("owner", hash_password(PASSWORD))
        store.close()
        self.app = create_app(c, FakeFlickr(), lambda: FakeVision(), background=False, scheduler=False)
        self.c = TestClient(self.app, base_url="http://testserver")
        self.csrf = None

    def post(self, path, body):
        return self.c.post(f"/api/music/explore/{path}", json=body, headers={"X-CSRF-Token": self.csrf or ""})

    def test_signed_in_only(self):
        self.assertEqual(self.c.get("/api/music/explore/facets").status_code, 401)

    def test_the_queries(self):
        self.csrf = self.c.post("/api/auth/login", json={"username": "owner", "password": PASSWORD}).json()["csrf"]
        self.app.state.runtime.explorer._whole, self.app.state.runtime.explorer._key = whole(), self.app.state.runtime.store().music_graph_version()
        self.assertEqual(self.c.get("/api/music/explore/facets").json()["artists"][0]["id"], "Haggard")
        self.assertEqual(self.post("neighbourhood", {"artist": "Owens", "have": ["Haggard"]}).json()["focus"], "Owens")
        self.assertEqual(self.post("neighbourhood", {"artist": "Nobody"}).status_code, 404)
        self.assertEqual(self.post("filter", {"scene": "hard bop"}).json()["matched"], 1)
        self.assertEqual(self.post("filter", {}).status_code, 400)
        self.assertEqual(self.post("path", {"a": "Haggard", "b": "Parsons"}).json()["path"], ["Haggard", "Owens", "Parsons"])
        self.assertEqual(self.post("path", {"a": "Haggard", "b": "Coltrane"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
