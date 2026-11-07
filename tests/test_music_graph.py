"""The artist network (music_graph.py): who's linked and why, the clusters, the bridges, and asking Claude."""
import json
import os
import tempfile
import unittest

from coconut_barometer import music_graph as G
from coconut_barometer.store import Store


def play(at, artist, name="Song", composer="", genre="Rock"):
    return {"played_at": at, "ms": 180000, "artist": artist, "name": name, "composer": composer, "genres": json.dumps([genre])}


def session(start, *artists):
    return [play(start + i * 200, a) for i, a in enumerate(artists)]


class Credits(unittest.TestCase):
    def test_featuring_and_title(self):
        self.assertEqual(G.credited("A feat. B & C", "x", {"A"}), ("A", ["B", "C"]))
        self.assertEqual(G.credited("A", "Song (with B)", {"A"}), ("A", ["B"]))

    def test_ampersand_is_one_act_unless_both_are_artists(self):
        self.assertEqual(G.credited("Simon & Garfunkel", "x", {"Simon & Garfunkel"}), ("Simon & Garfunkel", []))
        self.assertEqual(G.credited("Drake & Future", "x", {"Drake", "Future", "Drake & Future"}), ("Drake & Future", []))
        self.assertEqual(G.credited("Drake & Future", "x", {"Drake", "Future"}), ("Drake", ["Future"]))

    def test_generic_credits_dropped(self):
        self.assertEqual(G.composers("Traditional, Jo Writer"), ["Jo Writer"])


class Build(unittest.TestCase):
    def rows(self):
        out = []
        for n in range(4):                           # A, B, C work together; X, Y, Z work together; once across
            out += session(1000 + n * 10000, "A", "B", "C")
            out += session(500000 + n * 10000, "X", "Y", "Z")
        for a, b in (("A", "B"), ("B", "C"), ("C", "A"), ("X", "Y"), ("Y", "Z"), ("Z", "X"), ("A", "X")):
            out.append(play(800000 + len(out), a, f"Hit (feat. {b})"))
        out += [play(1_000_000, "M", "One (with B)"), play(1_000_100, "M", "Two (with Y)"), play(1_000_200, "M", composer="A")]
        return sorted(out, key=lambda r: r["played_at"])           # M bridges both worlds

    def test_edges_and_clusters(self):
        g = G.build(self.rows())
        by = {(e["a"], e["b"]): e for e in g["edges"]}
        self.assertEqual(by[("A", "X")]["kinds"]["collab"], 1)
        self.assertIn("credit", by[("A", "M")]["kinds"])
        group = {n["id"]: n["cluster"] for n in g["nodes"]}
        self.assertEqual(group["A"], group["B"])
        self.assertEqual(group["X"], group["Y"])

    def test_bridge_is_the_artist_between_clusters(self):
        g = G.build(self.rows())
        self.assertIn("M", [b["id"] for b in g["bridges"]])

    def test_playing_back_to_back_links_nothing(self):
        g = G.build([r for n in range(10) for r in session(n * 1000, "A", "B", "C")])
        self.assertEqual(g["edges"], [])

    def test_similar_links_and_labels(self):
        rows = self.rows()
        base = G.build(rows)
        key = base["clusters"][0]["key"]
        g = G.build(rows, similar=[{"a": "A", "b": "Z", "why": "same scene"}, {"a": "A", "b": "Nobody", "why": "x"}], labels={key: "Test sound"})
        self.assertEqual(next(e for e in g["edges"] if (e["a"], e["b"]) == ("A", "Z"))["kinds"]["similar"], "same scene")
        self.assertNotIn("Nobody", [n["id"] for n in g["nodes"]])
        self.assertIn("Test sound", [c["label"] for c in g["clusters"]])

    def test_limit(self):
        self.assertEqual(len(G.build(self.rows(), limit=3)["nodes"]), 3)


class Ask(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.duckdb"))
        for i, r in enumerate(Build().rows()):
            tid = f"{r['artist']}{i}"
            self.store.db.execute("INSERT INTO music_tracks (id, kind, name, artist, album, genres, composer) VALUES (?,?,?,?,?,?,?)",
                                  (tid, "songs", r["name"], r["artist"], "", r["genres"], r["composer"]))
            self.store.db.execute("INSERT INTO music_plays (track_id, played_at, seen_at, source) VALUES (?,?,?, 'export')", (tid, r["played_at"], r["played_at"]))

    def test_needs_a_key(self):
        self.assertFalse(G.can_ask({"vision": {"provider": "claude", "api_key": ""}}))
        with self.assertRaises(G.GraphError):
            G.ask_similar({"vision": {"provider": "ollama", "api_key": "x"}}, self.store)

    def test_ask_keeps_only_known_names_and_labels_clusters(self):
        replies = []

        def fake(cfg, prompt, tool, schema):
            replies.append(tool)
            if tool == "record_links":
                return {"links": [{"a": "A", "b": "Z", "why": "scene"}, {"a": "A", "b": "Ghost", "why": "?"}]}
            ids = [line.split(":")[0] for line in prompt.splitlines() if ": " in line]
            return {"labels": [{"id": i, "label": "Named"} for i in ids] + [{"id": "bogus", "label": "No"}]}
        real, G._ask = G._ask, fake
        try:
            g = G.ask_similar({"vision": {"provider": "claude", "api_key": "k", "model": "m"}}, self.store)
        finally:
            G._ask = real
        self.assertEqual(replies, ["record_links", "record_labels"])
        self.assertTrue(g["ai"]["asked"])
        self.assertEqual(g["ai"]["links"], 1)
        self.assertIn("Named", [c["label"] for c in g["clusters"]])
        self.assertEqual(G.report(self.store, 3650)["ai"]["links"], 1)            # kept
        self.assertNotIn("Ghost", self.store.get_meta(G.META_SIMILAR))


if __name__ == "__main__":
    unittest.main()
