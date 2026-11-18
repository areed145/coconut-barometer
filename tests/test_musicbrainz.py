"""MusicBrainz credits (#192): reading its answers, matching recordings to tracks, the lookups as rate-limited jobs on
the worker, and the graph's "musicbrainz" edges."""
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from coconut_barometer import local
from coconut_barometer import music_graph as G
from coconut_barometer import musicbrainz as MB
from coconut_barometer.config import from_dict
from coconut_barometer.ratelimit import SharedRateLimiter
from coconut_barometer.store import Store
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import hash_password

PASSWORD = "correct horse battery"
ON = {"musicbrainz": {"enabled": True, "contact": "me@example.com"}}


def ac(*names):
    return [{"name": n, "joinphrase": "", "artist": {"id": f"id-{n}", "name": n}} for n in names]


RECORDING = {"id": "rec-1", "title": "Hit", "artist-credit": ac("Band", "Guest"), "relations": [
    {"type": "producer", "target-type": "artist", "artist": {"id": "id-Prod", "name": "Prod"}},
    {"type": "mix", "target-type": "artist", "artist": {"id": "id-Mixer", "name": "Mixer"}},
    {"type": "tribute", "target-type": "artist", "artist": {"id": "id-Fan", "name": "Fan"}},              # not a credit we keep
    {"type": "performance", "target-type": "work", "work": {"relations": [
        {"type": "composer", "target-type": "artist", "artist": {"id": "id-Writer", "name": "Writer"}}]}}]}


class Answer:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


class FakeMB:
    """MusicBrainz's API by path: isrc/<code>, recording (search), recording/<id>, artist/<id>."""

    def __init__(self, pages, status=None):
        self.pages, self.status, self.calls = pages, status or {}, []

    def __call__(self, url, params, timeout, headers):
        path = url.split("/ws/2/")[1]
        self.calls.append((path, params, headers))
        if path in self.status:
            return Answer({}, self.status[path])
        return Answer(self.pages[path]) if path in self.pages else Answer({}, 404)


def limiter():
    return SharedRateLimiter(os.path.join(tempfile.mkdtemp(), "mb.lock"), 0, 1000.0)


class Reading(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(MB.norm("The Beatles"), MB.norm("beatles"))
        self.assertEqual(MB.norm("Simon & Garfunkel"), MB.norm("Simon and Garfunkel"))
        self.assertEqual(MB.norm("Beyoncé"), "beyonce")

    def test_credits(self):
        got = {(c["role"], c["name"]) for c in MB.credits(RECORDING)}
        self.assertEqual(got, {("artist", "Band"), ("featured", "Guest"), ("producer", "Prod"), ("writer", "Writer"),
                               ("mixer", "Mixer")})

    def test_credits_keep_what_each_person_did(self):
        rel = lambda t, name, *attrs: {"type": t, "target-type": "artist", "attributes": list(attrs),
                                       "artist": {"id": f"id-{name}", "name": name}}
        rec = {"artist-credit": ac("Souther"), "relations": [
            rel("instrument", "Gold", "guitar"), rel("instrument", "Gold", "piano"), rel("vocal", "Gold", "background vocals"),
            rel("vocal", "Ronstadt"), rel("producer", "Asher", "co"), rel("producer", "Exec", "executive"),
            rel("instrument", "Guerin", "drums (drum set)", "additional"), rel("mix", "Mixer"), rel("recording", "Eng"),
            rel("engineer", "Eng", "assistant"), rel("mastering", "Master"),
            {"type": "performance", "target-type": "work", "work": {"relations": [
                {"type": "lyricist", "target-type": "artist", "artist": {"id": "id-Souther", "name": "Souther"}}]}}]}
        got = {(c["role"], c["name"]): c["detail"] for c in MB.credits(rec)}
        self.assertEqual(got, {("artist", "Souther"): "", ("performer", "Gold"): "guitar, piano, background vocals",
                               ("performer", "Ronstadt"): "vocals", ("producer", "Asher"): "co-producer",
                               ("producer", "Exec"): "executive producer",
                               ("performer", "Guerin"): "additional drums (drum set)", ("writer", "Souther"): "lyricist",
                               ("mixer", "Mixer"): "mixing", ("engineer", "Eng"): "recording engineer, assistant engineer",
                               ("engineer", "Master"): "mastering"})

    def test_artist_rels_keep_the_direction(self):
        rels = MB.artist_rels({"relations": [
            {"type": "member of band", "direction": "backward", "target-type": "artist", "artist": {"id": "p", "name": "Person"}},
            {"type": "member of band", "direction": "forward", "target-type": "artist", "artist": {"id": "b", "name": "Big Band"}},
            {"type": "tribute", "direction": "backward", "target-type": "artist", "artist": {"id": "x", "name": "Copy Band"}}]})
        self.assertEqual([(r["role"], r["name"]) for r in rels], [("band of", "Person"), ("in band", "Big Band")])

    def test_artist_rels_keep_who_plays_with_whom_and_family(self):
        rel = lambda t, d, name: {"type": t, "direction": d, "target-type": "artist", "artist": {"id": name.lower(), "name": name}}
        rels = MB.artist_rels({"relations": [
            rel("instrumental supporting musician", "backward", "Drummer"), rel("vocal supporting musician", "forward", "Star"),
            rel("is person", "forward", "Real Name"), rel("sibling", "forward", "Sis"), rel("involved with", "forward", "Ex"),
            rel("subgroup", "forward", "Offshoot"), rel("named after artist", "backward", "Fan")]})
        self.assertEqual([(r["role"], r["name"]) for r in rels], [
            ("supported by", "Drummer"), ("supports", "Star"), ("same person", "Real Name"), ("sibling", "Sis"),
            ("partner", "Ex"), ("subgroup", "Offshoot")])

    def test_artist_facts(self):
        f = MB.artist_facts({"type": "Group", "country": "US", "area": {"name": "United States"},
                             "begin-area": {"name": "Bakersfield"}, "life-span": {"begin": "1966-03", "end": "1979"},
                             "genres": [{"name": "Country", "count": 2}, {"name": "country rock", "count": 5}, {"name": "x", "count": 0}],
                             "tags": [{"name": "seen live", "count": 1}]})
        self.assertEqual(f, {"type": "Group", "country": "US", "area": "Bakersfield", "begin_year": 1966, "end_year": 1979,
                             "genres": [["country rock", 5], ["country", 2]], "tags": [["seen live", 1]]})
        self.assertEqual(MB.artist_facts({"area": {"name": "United States"}})["area"], None)    # a country isn't a city
        self.assertEqual(MB.artist_facts({})["genres"], [])

    def test_pick_needs_the_same_main_artist(self):
        self.assertEqual(MB.pick([{"id": "1", "artist-credit": ac("Other")}, {"id": "2", "artist-credit": ac("The Band")}], "Band")["id"], "2")
        self.assertIsNone(MB.pick([{"id": "1", "artist-credit": ac("Other")}], "Band"))

    def test_ready_needs_a_contact(self):
        self.assertFalse(MB.ready({"musicbrainz": {"enabled": True, "contact": ""}}))
        self.assertFalse(MB.ready({"musicbrainz": {"enabled": False, "contact": "x"}}))
        self.assertTrue(MB.ready(ON))
        self.assertIn("( me@example.com )", MB.user_agent(ON))


class Lookups(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
        rows = [("t1", "Hit (feat. Guest)", "Band feat. Guest", "ISRC1", 5), ("t2", "Other", "Band", None, 1), ("t3", "Nope", "Nobody", None, 3)]
        for tid, name, artist, isrc, plays in rows:
            self.store.db.execute("INSERT INTO music_tracks (id, kind, name, artist, genres, isrc, enriched) VALUES (?, 'songs', ?, ?, '[]', ?, 1)",
                                  (tid, name, artist, isrc))
            for i in range(plays):
                self.store.db.execute("INSERT INTO music_plays (track_id, played_at, seen_at, source) VALUES (?, ?, ?, 'export')", (tid, i, i))
        self.api = FakeMB({
            "isrc/ISRC1": {"recordings": [{"id": "rec-1", "artist-credit": ac("Band", "Guest")}]},
            "recording/rec-1": RECORDING, "recording/rec-2": {"id": "rec-2", "artist-credit": ac("Band")},
            "recording": {"recordings": [{"id": "rec-2", "score": 100, "artist-credit": ac("Band")}]},
            "artist/id-Band": {"name": "Band", "type": "Group", "country": "GB", "life-span": {"begin": "1990"},
                               "genres": [{"name": "britpop", "count": 3}], "relations": [
                {"type": "member of band", "direction": "backward", "target-type": "artist", "artist": {"id": "id-Prod", "name": "Prod"}}]},
        })
        self.mb = MB.MusicBrainz(ON, limiter=limiter(), get=self.api, sleep=lambda s: None)

    def test_a_job_looks_up_tracks_most_played_first_then_their_artists(self):
        out = self.mb.run_once(self.store, now=100)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["rows"], 3)
        paths = [p for p, _, _ in self.api.calls]
        self.assertEqual(paths[:2], ["isrc/ISRC1", "recording/rec-1"])                 # t1: 5 plays, has an ISRC
        self.assertIn("artist/id-Band", paths)
        self.assertTrue(all("me@example.com" in h["User-Agent"] for _, _, h in self.api.calls))
        self.assertEqual(self.store.mb_progress(), {"asked": 3, "found": 2, "tracks": 3, "artists": 2, "rereading": 0})
        self.assertEqual(self.store.mb_to_ask(10), [])
        self.assertFalse(self.mb.due(self.store))
        self.api.calls.clear()
        self.mb.run_once(self.store, now=200)                                          # nothing is asked twice
        self.assertEqual(self.api.calls, [])

    def test_an_artist_brings_its_facts(self):
        self.mb.run_once(self.store, now=100)
        params = next(p for path, p, _ in self.api.calls if path == "artist/id-Band")
        self.assertEqual(params["inc"], "artist-rels+genres+tags")
        band = self.store.mb_graph()["artists"]["id-Band"]
        self.assertEqual((band["type"], band["country"], band["begin_year"], band["genres"]), ("Group", "GB", 1990, [["britpop", 3]]))

    def test_an_artist_looked_up_before_facts_were_kept_is_asked_again(self):
        self.mb.run_once(self.store, now=100)
        self.store.db.execute("UPDATE mb_artists SET profiled = NULL WHERE mbid = 'id-Band'")    # as the upgrade leaves it
        self.assertEqual([a["mbid"] for a in self.store.mb_artists_to_ask(10)], ["id-Band"])
        self.assertNotIn("id-Band", self.store.mb_graph()["artists"])
        self.mb.run_once(self.store, now=200)
        self.assertEqual(self.store.mb_artists_to_ask(10), [])

    def test_the_upgrade_asks_about_every_artist_again_and_drops_the_old_ai_links(self):
        self.mb.run_once(self.store, now=100)
        self.store.db.execute("DROP TABLE mb_artists")
        self.store.db.execute("CREATE TABLE mb_artists (mbid TEXT PRIMARY KEY, name TEXT, asked BIGINT)")   # as v17 had it
        self.store.db.execute("INSERT INTO mb_artists VALUES ('id-Band', 'Band', 100)")
        self.store.set_meta("music_graph_similar", "[]")
        self.store.set_meta("schema_version", 17)
        self.store.migrate()
        self.assertIn("id-Band", [a["mbid"] for a in self.store.mb_artists_to_ask(10)])
        self.assertIsNone(self.store.get_meta("music_graph_similar"))

    def test_search_strips_featuring_and_needs_the_same_artist(self):
        self.mb.run_once(self.store, now=1)
        search = next(p for path, p, _ in self.api.calls if path == "recording")
        self.assertIn('recording:"', search["query"])
        self.assertNotIn("feat", search["query"].lower())

    def test_an_artist_looked_up_for_fewer_relations_is_asked_again_after_new_ones(self):
        self.mb.run_once(self.store, now=100)
        self.assertEqual(self.store.mb_artists_to_ask(10), [])
        self.store.db.execute("UPDATE mb_artists SET rels_v = NULL WHERE mbid = 'id-Band'")    # as the upgrade leaves it
        self.assertEqual([a["mbid"] for a in self.store.mb_artists_to_ask(10)], ["id-Band"])
        self.assertIn("id-Band", self.store.mb_graph()["artists"])                             # its facts stay meanwhile
        self.store.db.execute("INSERT INTO mb_credits (track_id, role, name, mbid) VALUES ('t1', 'featured', 'New', 'id-New')")
        self.assertEqual([a["mbid"] for a in self.store.mb_artists_to_ask(10)], ["id-New", "id-Band"])   # never asked first
        self.mb.run_once(self.store, now=200)
        self.assertEqual(self.store.mb_artists_to_ask(10), [])

    def test_tracks_found_before_roles_were_kept_are_read_again_by_their_recording(self):
        self.mb.run_once(self.store, now=100)
        self.store.db.execute("UPDATE mb_recordings SET credits_v = NULL")                       # as the upgrade leaves it
        self.assertEqual(self.store.mb_progress()["rereading"], 2)
        again = self.store.mb_to_ask(10)
        self.assertEqual([(t["id"], t["recording"]) for t in again], [("t1", "rec-1"), ("t2", "rec-2")])
        self.api.calls.clear()
        self.mb.run_once(self.store, now=200)
        self.assertEqual([p for p, _, _ in self.api.calls], ["recording/rec-1", "recording/rec-2"])   # no ISRC, no search
        self.assertEqual(self.store.mb_progress()["rereading"], 0)
        self.assertEqual(self.store.mb_to_ask(10), [])

    def test_the_graph_and_the_questions_get_each_track_and_role(self):
        self.mb.run_once(self.store, now=100)
        self.store.db.execute("UPDATE music_tracks SET album = 'Album One' WHERE id = 't1'")
        prod = next(c for c in self.store.mb_graph()["credits"] if c["role"] == "producer")
        self.assertEqual(prod["items"], [{"track": "Hit (feat. Guest)", "album": "Album One", "detail": "producer"}])
        self.assertTrue(all(c["items"] is None for c in self.store.mb_graph()["credits"] if c["role"] == "artist"))
        found = self.store.mb_track_credits(title="hit feat guest")
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["track"], found[0]["album"], found[0]["looked_up"], found[0]["found"]),
                         ("Hit (feat. Guest)", "Album One", True, True))
        self.assertIn({"name": "Prod", "role": "producer", "detail": "producer"}, found[0]["credits"])
        self.assertEqual(found[0]["credits"][0]["role"], "artist")
        self.assertEqual([t["track"] for t in self.store.mb_track_credits(album="album one")], ["Hit (feat. Guest)"])
        nope = self.store.mb_track_credits(title="Nope")[0]
        self.assertEqual((nope["found"], nope["credits"]), (False, []))
        self.assertEqual(self.store.mb_track_credits(title="!!"), [])                       # nothing to match by

    def test_the_upgrade_keeps_credits_and_marks_them_for_reading_again(self):
        self.mb.run_once(self.store, now=100)
        self.store.db.execute("ALTER TABLE mb_credits DROP COLUMN detail")
        self.store.db.execute("ALTER TABLE mb_recordings DROP COLUMN credits_v")
        self.store.set_meta("schema_version", 19)
        self.store.migrate()
        self.assertEqual(self.store.mb_progress()["rereading"], 2)
        self.assertTrue(self.store.mb_graph()["credits"])

    def test_a_job_stops_at_its_budget(self):
        out = self.mb.run_once(self.store, budget=3, now=1)
        self.assertLessEqual(out["calls"], 3)
        self.assertTrue(self.mb.due(self.store))

    def test_busy_stops_the_job_and_keeps_what_it_found(self):
        self.api.status["recording"] = 503
        out = self.mb.run_once(self.store, now=1)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["rows"], 1)                          # t1 went through before the search failed
        self.assertEqual(len([p for p, _, _ in self.api.calls if p == "recording"]), 3)     # tried again, twice

    def test_every_request_waits_for_the_limiter(self):
        waits = []

        class Counting(SharedRateLimiter):
            def wait(self, on_slot=None):
                waits.append(1)
                return 0.0
        self.mb.limiter = Counting("unused", 0, 1.0)
        out = self.mb.run_once(self.store, now=1)
        self.assertEqual(len(waits), out["calls"])

    def test_off_does_nothing(self):
        mb = MB.MusicBrainz({"musicbrainz": {"enabled": False, "contact": "x"}}, limiter=limiter(), get=self.api)
        self.assertEqual(mb.run_once(self.store)["status"], "skipped")
        self.assertEqual(self.api.calls, [])


class Edges(unittest.TestCase):
    def rows(self):
        out = []
        for i, a in enumerate(["Band", "Prod", "Guest", "Solo", "Other"] * 3):
            out.append({"played_at": i * 100000, "ms": 1000, "artist": a, "name": "x", "composer": "", "genres": "[]"})
        return out

    def test_credits_link_by_mbid_and_by_unambiguous_name(self):
        mb = {"credits": [
            {"artist": "Band", "role": "artist", "name": "Band", "mbid": "id-Band", "tracks": 3},
            {"artist": "Prod", "role": "artist", "name": "Prod", "mbid": "id-Prod", "tracks": 3},
            {"artist": "Band", "role": "producer", "name": "Prod", "mbid": "id-Prod", "tracks": 2},
            {"artist": "Band", "role": "featured", "name": "guest", "mbid": "id-Guest", "tracks": 1},    # by name
            {"artist": "Band", "role": "writer", "name": "Ghost Writer", "mbid": "id-GW", "tracks": 1},
            {"artist": "Solo", "role": "writer", "name": "Ghost Writer", "mbid": "id-GW", "tracks": 1},
            {"artist": "Other", "role": "producer", "name": "Prod", "mbid": "id-SomeoneElse", "tracks": 1}],  # same name, not him
            "rels": [{"mbid": "id-Band", "name": "Band", "role": "band of", "other": "id-Prod", "other_name": "Prod"}]}
        g = G.build(self.rows(), mb=mb)
        by = {(e["a"], e["b"]): [r["said"] for r in e["rels"]] for e in g["edges"]}
        self.assertEqual(by[("Band", "Prod")], ["Prod is in Band", "Prod produced Band (2 tracks)"])   # strongest first
        self.assertEqual(by[("Band", "Guest")], ["Guest featured on Band"])
        self.assertEqual(by[("Band", "Solo")], ["both have songs by Ghost Writer"])
        self.assertNotIn(("Other", "Prod"), by)

    def test_links_name_the_records_and_roles_behind_them(self):
        item = lambda track, detail, album="LP": {"track": track, "album": album, "detail": detail}
        mb = {"credits": [
            {"artist": "Band", "role": "artist", "name": "Band", "mbid": "id-Band", "tracks": 3, "items": None},
            {"artist": "Prod", "role": "artist", "name": "Prod", "mbid": "id-Prod", "tracks": 3, "items": None},
            {"artist": "Band", "role": "performer", "name": "Prod", "mbid": "id-Prod", "tracks": 2,
             "items": [item("One", "guitar"), item("Two", "lead vocals")]},
            {"artist": "Band", "role": "performer", "name": "Session", "mbid": "id-S", "tracks": 1, "items": [item("One", "drums")]},
            {"artist": "Solo", "role": "performer", "name": "Session", "mbid": "id-S", "tracks": 1,
             "items": [item("Lone", "drums, percussion", "Solo LP")]},
            {"artist": "Guest", "role": "artist", "name": "Guest", "mbid": "id-Guest", "tracks": 3, "items": None},
            {"artist": "Band", "role": "mixer", "name": "Guest", "mbid": "id-Guest", "tracks": 1, "items": [item("Two", "mixing")]},
            {"artist": "Band", "role": "engineer", "name": "Desk", "mbid": "id-D", "tracks": 1, "items": [item("One", "recording engineer")]},
            {"artist": "Other", "role": "engineer", "name": "Desk", "mbid": "id-D", "tracks": 1, "items": [item("Far", "mastering", "B")]}],
              "rels": []}
        by = {(e["a"], e["b"]): e["rels"] for e in G.build(self.rows(), mb=mb)["edges"]}
        played = by[("Band", "Prod")][0]
        self.assertEqual(played["said"], "Prod played on Band (2 tracks)")
        self.assertEqual(played["credits"], [{"artist": "Band", "track": "One", "album": "LP", "who": "Prod", "role": "guitar"},
                                             {"artist": "Band", "track": "Two", "album": "LP", "who": "Prod", "role": "lead vocals"}])
        self.assertEqual(G.credit_line(played["credits"][0]), "One (LP, by Band): Prod, guitar")
        mixed = by[("Band", "Guest")][0]
        self.assertEqual((mixed["said"], mixed["credits"][0]["role"]), ("Guest mixed Band", "mixing"))
        desk = by[("Band", "Other")][0]
        self.assertEqual((desk["type"], desk["said"]), ("shared engineer", "both engineered by Desk"))
        self.assertEqual([c["role"] for c in desk["credits"]], ["recording engineer", "mastering"])
        shared = by[("Band", "Solo")][0]
        self.assertEqual(shared["said"], "Session played on both")
        self.assertEqual([(c["artist"], c["track"], c["role"]) for c in shared["credits"]],
                         [("Band", "One", "drums"), ("Solo", "Lone", "drums, percussion")])

    def test_artist_relations_are_typed(self):
        mb = {"credits": [{"artist": a, "role": "artist", "name": a, "mbid": f"id-{a}", "tracks": 3} for a in ("Band", "Prod", "Guest", "Solo", "Other")],
              "rels": [{"mbid": "id-Solo", "name": "Solo", "role": "sibling", "other": "id-Other", "other_name": "Other"},
                       {"mbid": "id-Guest", "name": "Guest", "role": "supports", "other": "id-Band", "other_name": "Band"},
                       {"mbid": "id-Prod", "name": "Prod", "role": "same person", "other": "id-Solo", "other_name": "Solo"},
                       {"mbid": "id-Band", "name": "Band", "role": "band of", "other": "id-Drummer", "other_name": "Drummer"},
                       {"mbid": "id-Other", "name": "Other", "role": "in band", "other": "id-Drummer", "other_name": "Drummer"}]}
        by = {(e["a"], e["b"]): e["rels"] for e in G.build(self.rows(), mb=mb)["edges"]}
        said = {pair: [(r["type"], r["said"]) for r in rels] for pair, rels in by.items()}
        self.assertEqual(said[("Other", "Solo")], [("family", "Other and Solo are siblings")])
        self.assertEqual(said[("Band", "Guest")], [("played", "Guest played with Band")])
        self.assertEqual(by[("Band", "Guest")][0]["from"], "Guest")                          # the arrow's direction
        self.assertEqual(said[("Prod", "Solo")], [("same person", "Prod and Solo are the same artist")])
        self.assertEqual(said[("Band", "Other")], [("shared member", "Drummer is in both")])


class Jobs(unittest.TestCase):
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

    def send(self):
        return self.c.post("/api/music/graph/musicbrainz", headers={"X-CSRF-Token": self.csrf})

    def test_lookups_go_to_the_back_of_the_worker_line_one_at_a_time(self):
        self.assertEqual(self.send().json(), {"status": "not_ready"})
        self.rt.cfg["musicbrainz"].update(ON["musicbrainz"])
        self.rt.review.jobs.put(("sync", ""))
        self.assertEqual(self.send().json(), {"status": "queued"})
        self.assertEqual(self.send().json(), {"status": "busy"})
        self.assertEqual(list(self.rt.review.jobs.queue), [("sync", ""), ("musicbrainz", "")])
        self.assertTrue(self.rt.review.jobs_view()["services"]["musicbrainz"]["configured"])

    def test_held_in_local_mode(self):
        local.set_on(True)
        self.addCleanup(local.set_on, False)
        self.assertEqual(self.send().status_code, 409)

    def test_the_graph_says_how_far_the_lookups_are(self):
        g = self.c.get("/api/music/graph", params={"limit": 50}).json()
        self.assertEqual(g["musicbrainz"]["enabled"], False)
        self.assertEqual(self.c.get("/api/music/graph", params={"limit": 5}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
