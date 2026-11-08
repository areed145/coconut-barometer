"""The artist network (music_graph.py): who's linked and why, the clusters, the bridges, and asking Claude."""
import json
import os
import tempfile
import unittest
from collections import Counter

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
        self.assertEqual([(r["type"], r["said"], r["via"]) for r in by[("A", "X")]["rels"]], [("featured", "X featured on A", ["song title"])])
        self.assertEqual([(r["type"], r["from"], r["said"]) for r in by[("A", "M")]["rels"]], [("wrote", "A", "A wrote for M")])
        self.assertEqual(g["types"]["member"], {"kind": "band", "pull": 3.0, "rest": 34})
        group = {n["id"]: n["cluster"] for n in g["nodes"]}
        self.assertEqual(group["A"], group["B"])
        self.assertEqual(group["X"], group["Y"])

    def test_bridge_is_the_artist_between_clusters(self):
        g = G.build(self.rows())
        self.assertIn("M", [b["id"] for b in g["bridges"]])

    def test_playing_back_to_back_links_nothing(self):
        g = G.build([r for n in range(10) for r in session(n * 1000, "A", "B", "C")])
        self.assertEqual(g["edges"], [])

    def test_each_relationship_pulls_by_what_it_is(self):
        self.assertGreater(G.pull({"type": "member", "tracks": 1}), G.pull({"type": "featured", "tracks": 1}))
        self.assertGreater(G.pull({"type": "featured", "tracks": 1}), G.pull({"type": "shared producer", "tracks": 1}))
        self.assertGreater(G.pull({"type": "shared player", "tracks": 1}), G.pull({"type": "sound", "tracks": 1, "score": 0.9}))
        self.assertEqual(G.pull({"type": "featured", "tracks": 4}), 3.0)                    # more tracks, up to 1.75×
        self.assertEqual(G.pull({"type": "featured", "tracks": 1000}), 3.5)
        self.assertEqual(G.pull({"type": "sound", "tracks": 1, "score": 0.45}), 0.125)

    def test_one_relationship_found_twice_is_one_and_a_pair_adds_up(self):
        rows = [play(i, a) for i, a in enumerate(["A", "B"] * 3)] + [play(10, "A", "Hit (feat. B)"), play(11, "B", "Other (feat. A)")]
        mb = {"credits": [{"artist": "A", "role": "artist", "name": "A", "mbid": "id-A", "tracks": 3},
                          {"artist": "B", "role": "artist", "name": "B", "mbid": "id-B", "tracks": 3},
                          {"artist": "A", "role": "featured", "name": "B", "mbid": "id-B", "tracks": 1},
                          {"artist": "A", "role": "producer", "name": "B", "mbid": "id-B", "tracks": 1}],
              "rels": [{"mbid": "id-B", "name": "B", "role": "in band", "other": "id-A", "other_name": "A"}]}
        e = G.build(rows, mb=mb)["edges"][0]
        self.assertEqual([(r["type"], r["said"], r["via"]) for r in e["rels"]], [
            ("member", "B is in A", ["MusicBrainz"]),
            ("featured", "A and B on 2 tracks together", ["song title", "MusicBrainz"]),   # both ways, title and MusicBrainz
            ("produced", "B produced A", ["MusicBrainz"])])
        self.assertEqual([r["pull"] for r in e["rels"]], [3.0, 2.5, 1.5])                  # 2 tracks: 2 × 1.25
        self.assertEqual(e["weight"], G.MAX_PULL)                                          # 3 + 0.3 × 4, held to 4

    def test_limit(self):
        self.assertEqual(len(G.build(self.rows(), limit=3)["nodes"]), 3)


def prof(genres=None, scenes=(), span=None, city=None, country=None):
    return {"genres": genres or {}, "scenes": set(scenes), "span": span, "city": city, "country": country}


class Profiles(unittest.TestCase):
    def test_a_rare_scene_and_style_link_and_say_why(self):
        p = {"Haggard": prof({"rock": 1, "country rock": 1}, ["bakersfield sound"], (1960, 1979), "Bakersfield", "US"),
             "Owens": prof({"rock": 1, "country rock": 1}, ["bakersfield sound"], (1955, 1975), "Bakersfield", "US"),
             "Other": prof({"rock": 1, "techno": 1}, [], (1990, 1999), None, "DE")}
        links = G.profile_links(p)
        self.assertEqual(list(links), [("Haggard", "Owens")])
        self.assertEqual(links[("Haggard", "Owens")][1], "bakersfield sound · country rock · 1960s–70s · Bakersfield")

    def test_a_scene_links_across_eras(self):
        p = {"Haggard": prof({"honky tonk": 1}, ["outlaw country"], (1960, 1979)),
             "Simpson": prof({"americana": 1}, ["outlaw country"], (2010, 2029)),
             "Other": prof({"techno": 1})}
        self.assertEqual(G.profile_links(p)[("Haggard", "Simpson")][1], "outlaw country")

    def test_a_genre_everyone_has_years_and_places_link_nothing(self):
        p = {a: prof({"rock": 1}, [], (1970, 1979), "Austin", "US") for a in "ABC"}
        p["D"] = prof({"jazz": 1})
        self.assertEqual(G.profile_links(p), {})

    def test_a_scene_most_of_them_share_links_nothing_either(self):
        p = {a: prof({f"genre {a}": 1}, ["indie"]) for a in "ABC"}
        p["D"] = prof({"jazz": 1})
        self.assertEqual(G.profile_links(p), {})
        p["A"]["scenes"].add("c86")
        p["B"]["scenes"].add("c86")                                             # a rarer one still does, and says so
        self.assertEqual(G.profile_links(p), {("A", "B"): (0.45, "c86", True)})

    def test_each_artist_keeps_its_closest_few(self):
        p = {f"A{i}": prof({"rock": 1, "surf": 1}, ["surf"], (1960, 1969)) for i in range(8)}
        p.update({f"Z{i}": prof({f"genre {i}": 1}) for i in range(30)})          # surf is rare among them all
        links = G.profile_links(p)
        per = {a: sum(a in pair for pair in links) for a in p}
        self.assertTrue(links)
        self.assertLessEqual(max(per.values()), G.PROFILE_PER_ARTIST)

    def test_profiles_take_musicbrainz_and_claude(self):
        mb = {"credits": [{"artist": "Band", "role": "artist", "name": "Band", "mbid": "m1", "tracks": 3},
                          {"artist": "Singer", "role": "artist", "name": "Singer", "mbid": "m2", "tracks": 3}],
              "artists": {"m1": {"type": "Group", "country": "US", "area": "Athens", "begin_year": 1980, "end_year": None,
                                 "genres": [["jangle pop", 4], ["rock", 2]], "tags": []},
                          "m2": {"type": "Person", "country": "US", "area": "Tulsa", "begin_year": 1950, "end_year": None,
                                 "genres": [], "tags": []}}}
        said = {"Singer": {"scenes": ["red dirt"], "styles": ["americana"], "decades": [1990, 2000], "country": "US"}}
        genres = {"Band": Counter({"Rock": 2}), "Singer": Counter()}
        p = G.artist_profiles(["Band", "Singer"], genres, mb, said, {"Band", "Singer"}, 2026)
        self.assertEqual(p["Band"]["span"], (1980, 2026))            # a group's years, still going
        self.assertEqual(p["Band"]["genres"]["jangle pop"], 1.0)
        self.assertAlmostEqual(p["Band"]["genres"]["rock"], 0.5 + 0.5)  # Apple's half, MusicBrainz's half
        self.assertEqual(p["Singer"]["span"], (1990, 2009))           # Claude's decades, not a birth year
        self.assertEqual((p["Singer"]["scenes"], p["Singer"]["city"]), ({"red dirt"}, "Tulsa"))

    def test_clusters_are_named_by_a_shared_scene_else_their_genres(self):
        p = {"A": prof({"surf": 1}, ["surf"]), "B": prof({"surf": 1}, ["surf"]), "C": prof({"hot rod": 1})}
        genres = {a: Counter({"Rock": 1}) for a in "ABC"}
        self.assertEqual(G.cluster_label(["A", "B", "C"], p, genres), "Surf")
        self.assertEqual(G.cluster_label(["C", "A"], {"A": prof({"surf": 1}), "C": prof({"hot rod": 2})}, genres), "Hot Rod / Surf")
        self.assertEqual(G.cluster_label(["C"], p, genres), "Rock")

    def test_the_graph_draws_profile_matches(self):
        rows = [play(i * 100, a, genre="Rock") for i, a in enumerate(["A", "B", "C"] * 3)]
        said = {a: {"scenes": ["paisley underground"], "styles": ["jangle pop"], "decades": [1980], "country": "US"} for a in "AB"}
        g = G.build(rows, profiles=said, now=1_800_000_000)
        self.assertEqual([(e["a"], e["b"], [r["type"] for r in e["rels"]]) for e in g["edges"]], [("A", "B", ["scene"])])


class Report(unittest.TestCase):
    def test_the_report_reads_profiles_from_the_store(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
        for i, a in enumerate(["A", "B", "C"] * 3):
            store.db.execute("INSERT INTO music_tracks (id, kind, name, artist, album, genres) VALUES (?, 'songs', 'x', ?, '', '[]') ON CONFLICT DO NOTHING", (a, a))
            store.db.execute("INSERT INTO music_plays (track_id, played_at, seen_at, source) VALUES (?, ?, ?, 'export')", (a, i, i))
        store.profiles_save([{"artist": a, "scenes": ["surf"], "styles": ["surf rock"], "decades": [1960], "country": "US"} for a in "AB"], "m", 1)
        g = G.report(store, 3650)
        self.assertEqual([(e["a"], e["b"]) for e in g["edges"]], [("A", "B")])
        self.assertNotIn("ai", g)


if __name__ == "__main__":
    unittest.main()
