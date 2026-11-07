"""Apple Music: the developer token Apple needs, working out new plays from lists with no times, the reads, the
report, and connecting (whose page alone may load Apple's script)."""
import base64
import csv
import gzip
import json
import os
import tempfile
import unittest

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from coconut_barometer import apple_export as X
from coconut_barometer import apple_music as M
from coconut_barometer.config import from_dict
from coconut_barometer.store import Store
from coconut_barometer.web import security
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import hash_password

PASSWORD = "correct horse battery"
KEY = ec.generate_private_key(ec.SECP256R1())
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
NOON = 1_790_000_000


def cfg(user_token="user-token", every=60):
    return {"apple_music": {"team_id": "TEAM123456", "key_id": "KEY1234567", "private_key": PEM, "user_token": user_token,
                            "every_minutes": every}}


def song(i, minutes=4):
    return {"id": i, "type": "songs", "attributes": {"name": f"Song {i}", "artistName": f"Artist {i}", "albumName": f"Album {i}",
                                                     "genreNames": ["Rock", "Music"], "durationInMillis": minutes * 60000}}


class Answer:
    def __init__(self, body=None, status=200):
        self.body, self.status_code = body or {"data": []}, status

    def json(self):
        return self.body


def answers(*ids, status=200):
    return lambda url, headers, timeout: Answer({"data": [song(i) for i in ids]}, status)


def unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class TokenTest(unittest.TestCase):
    def check(self, token):
        head, body, sig = token.split(".")
        raw = unb64(sig)
        self.assertEqual(len(raw), 64)                # r and s side by side, not DER
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        try:
            KEY.public_key().verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            self.fail("signature doesn't verify")
        return json.loads(unb64(head)), json.loads(unb64(body))

    def test_what_apple_asks_for(self):
        head, body = self.check(M.developer_token(cfg(), 600, now=NOON))
        self.assertEqual(head, {"alg": "ES256", "kid": "KEY1234567"})
        self.assertEqual(body, {"iss": "TEAM123456", "iat": NOON, "exp": NOON + 600})

    def test_a_key_pasted_into_a_one_line_field_still_works(self):
        c = cfg()
        c["apple_music"]["private_key"] = PEM.replace("\n", " ")
        self.check(M.developer_token(c))

    def test_bad_settings_say_so_without_the_key(self):
        c = cfg()
        c["apple_music"]["private_key"] = "not a key"
        with self.assertRaises(M.MusicError) as e:
            M.developer_token(c)
        self.assertNotIn("not a key", str(e.exception))
        with self.assertRaises(M.MusicError):
            M.developer_token({"apple_music": {"team_id": None, "key_id": None, "private_key": None}})


class NewPlaysTest(unittest.TestCase):
    def test_nothing_played(self):
        self.assertEqual(M.new_count(list("abc"), list("abc")), 0)

    def test_new_tracks_on_top(self):
        self.assertEqual(M.new_count(list("abc"), list("xyabc")), 2)

    def test_old_tracks_played_again_move_up(self):
        self.assertEqual(M.new_count(list("abc"), list("cab")), 1)
        self.assertEqual(M.new_count(list("abc"), list("bca")), 2)

    def test_a_repeat_the_list_keeps_twice(self):
        self.assertEqual(M.new_count(list("abc"), list("aabc")), 1)

    def test_the_end_falling_off_isnt_a_play(self):
        self.assertEqual(M.new_count(list("abcd"), list("xabc")), 1)

    def test_older_tracks_a_longer_list_reaches_arent_plays(self):
        self.assertEqual(M.new_count(list("ab"), list("abcd")), 0)
        self.assertEqual(M.new_count(list("ab"), list("xabcd")), 1)

    def test_everything_new(self):
        self.assertEqual(M.new_count(list("abc"), list("xyz")), 3)

    def test_times_stack_back_from_the_read_and_stop_at_the_last_one(self):
        self.assertEqual(M.play_times([100, 200], 0, 1000), [900, 700])
        self.assertEqual(M.play_times([500, 500, 500], 400, 1000), [500, 400, 400])


class ReadsTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.duckdb")
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.music = M.Music(cfg())

    def read(self, *ids, at=NOON):
        return self.music.poll(self.store, at, get=answers(*ids))

    def test_the_first_read_is_only_the_starting_point(self):
        self.assertEqual(self.read("a", "b"), 0)
        self.assertEqual(self.store.music_play_count(), 0)
        self.assertEqual(self.read("a", "b", at=NOON + 3600), 0)

    def test_new_plays_are_stored_with_their_window(self):
        self.read("a", "b")
        self.assertEqual(self.read("x", "a", "b", at=NOON + 3600), 1)
        p = self.store.music_plays(0)[0]
        self.assertEqual((p["name"], p["played_at"], p["seen_at"], p["window_start"]),
                         ("Song x", NOON + 3600 - 240, NOON + 3600, NOON))

    def test_a_track_left_out_once_isnt_new_when_it_comes_back(self):
        self.read("a", "b", "c")
        self.read("a", "c", at=NOON + 3600)
        self.assertEqual(self.read("a", "b", "c", at=NOON + 7200), 0)

    def test_a_repeat_the_list_keeps_twice_is_one_play_not_one_each_read(self):
        self.read("a", "b")
        self.assertEqual(self.read("a", "a", "b", at=NOON + 3600), 1)
        self.assertEqual(self.read("a", "a", "b", at=NOON + 7200), 0)

    def test_pages_are_followed_but_only_on_apples_api(self):
        seen = []

        def get(url, headers, timeout):
            seen.append(url)
            if len(seen) == 1:
                return Answer({"data": [song("a")], "next": "/v1/me/recent/played/tracks?offset=10"})
            return Answer({"data": [song("b")], "next": "https://elsewhere.example/steal"})
        self.assertEqual([i["id"] for i in M.fetch_recent(cfg(), get=get)], ["a", "b"])
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(u.startswith(M.API) for u in seen))

    def test_a_passing_failure_is_asked_again(self):
        codes = [504, 503, 200]

        def get(url, headers, timeout):
            return Answer({"data": [song("a")]}, codes.pop(0))
        waits = []
        self.assertEqual([i["id"] for i in M.fetch_recent(cfg(), get=get, sleep=waits.append)], ["a"])
        self.assertEqual(waits, list(M.RETRIES))

    def test_a_page_that_keeps_failing_loses_the_whole_read_and_it_retries_soon(self):
        calls = []

        def get(url, headers, timeout):
            calls.append(url)
            if len(calls) == 1:
                return Answer({"data": [song("a")], "next": "/v1/me/recent/played/tracks?offset=10"})
            return Answer(status=504)
        self.assertEqual(self.read("old"), 0)
        original = M.fetch_recent
        self.addCleanup(setattr, M, "fetch_recent", original)
        M.fetch_recent = lambda c, get: original(c, get=get, sleep=lambda _: None)
        self.music.run_once(self.store, NOON + 3600, get=get)
        self.assertEqual(self.store.music_play_count(), 0)
        s = self.music.state(self.store)
        self.assertIn("504", s["error"])
        self.assertFalse(s["reconnect"])
        self.assertFalse(self.music.due(self.store, NOON + 3600 + M.RETRY_READ_SECONDS - 1))
        self.assertTrue(self.music.due(self.store, NOON + 3600 + M.RETRY_READ_SECONDS))

    def test_a_refusal_isnt_asked_again(self):
        calls = []

        def get(url, headers, timeout):
            calls.append(url)
            return Answer(status=403)
        with self.assertRaises(M.NotAuthorized):
            M.fetch_recent(cfg(), get=get, sleep=self.fail)
        self.assertEqual(len(calls), 1)

    def test_the_tokens_go_where_apple_says(self):
        got = {}

        def get(url, headers, timeout):
            got.update(headers)
            return Answer()
        M.fetch_recent(cfg(), get=get)
        self.assertEqual(got["Music-User-Token"], "user-token")
        self.assertTrue(got["Authorization"].startswith("Bearer "))

    def test_a_full_list_of_new_plays_is_counted_as_a_loss(self):
        self.read("a")
        ids = [str(i) for i in range(50)]
        self.assertEqual(self.read(*ids, at=NOON + 3600), 50)
        self.assertEqual(self.music.state(self.store)["missed"], 1)

    def test_only_due_reads_happen_and_a_refusal_asks_to_connect_again(self):
        self.assertTrue(self.music.due(self.store, NOON))
        self.store.set_meta("music_last_poll", NOON)
        self.assertFalse(self.music.due(self.store, NOON + 3599))
        self.assertTrue(self.music.due(self.store, NOON + 3600))
        self.assertFalse(M.Music(cfg(user_token=None)).due(self.store, NOON + 3600))
        self.music.run_once(self.store, NOON + 3600, get=answers(status=403))
        s = self.music.state(self.store)
        self.assertTrue(s["reconnect"])
        self.assertIn("connect again", s["error"])

    def test_the_report(self):
        self.read("old")
        self.read("a", "b", "old", at=NOON + 3600)
        d = self.music.report(self.store, 7, "America/Los_Angeles", NOON + 3600)
        self.assertEqual(d["totals"], {"plays": 2, "minutes": 8, "tracks": 2, "artists": 2})
        self.assertEqual(d["genres"], [{"label": "Rock", "plays": 2}])        # "Music" tells nothing
        self.assertEqual(sum(map(sum, d["heat"])), 2)
        self.assertEqual(len(d["series"]), 8)
        self.assertEqual(sum(r["plays"] for r in d["series"]), 2)
        self.assertEqual(d["recent"][0]["name"], "Song a")

    def test_the_report_survives_a_nonsense_timezone(self):
        self.assertEqual(self.music.report(self.store, 7, "Not/AZone", NOON)["totals"]["plays"], 0)


ACTIVITY = ("Event Type", "Song Name", "Event Start Timestamp", "Play Duration Milliseconds", "Media Type", "Item Type", "End Reason Type",
            "Album Name", "Media Duration In Milliseconds", "UTC Offset In Seconds", "Container Type", "Feature Name", "IP Latitude", "Apple ID Number")
DAILY = ("Date Played", "Hours", "Track Description", "Track Reference", "Track Identifier")


def play(song, start="2024-03-01T10:15:00.000Z", heard=200000, event="PLAY_END", end="NATURAL_END_OF_TRACK", kind="ITUNES_STORE_CONTENT"):
    return (event, song, start, heard, "AUDIO", kind, end, "An Album", 210000, -25200, "PLAYLIST", "library / playlist_detail", "35.3", "12345678")


def write(path, head, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(rows)
    return path


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def slim(self, plays, daily):
        a = write(os.path.join(self.dir, "a.csv"), ACTIVITY, plays)
        d = write(os.path.join(self.dir, "d.csv"), DAILY, daily)
        out = os.path.join(self.dir, "out.csv.gz")
        X.slim(a, d, out, log=lambda _: None)
        with gzip.open(out, "rt", newline="") as f:
            return out, list(csv.DictReader(f))

    def test_only_real_plays_are_kept(self):
        _, rows = self.slim([play("Whole"), play("Whole"), play("Long enough", heard=31000, end="TRACK_SKIPPED_FORWARDS"),
                             play("Skipped", heard=5000, end="TRACK_SKIPPED_FORWARDS"), play("Started", event="PLAY_START"),
                             play("Not music", kind="INVALID")], [])
        self.assertEqual([r["song"] for r in rows], ["Whole", "Long enough"])

    def test_nothing_personal_is_kept(self):
        out, _ = self.slim([play("Song")], [])
        with gzip.open(out, "rt") as f:
            text = f.read()
        self.assertNotIn("35.3", text)
        self.assertNotIn("12345678", text)

    def test_the_same_hour_settles_a_title_two_artists_share(self):
        _, rows = self.slim([play("Home")], [("20240301", "9", "Wrong Band - Home", "111", "111"),
                                             ("20240301", "4, 10", "Right Band - Home", "222", "222"),
                                             ("20230101", "1", "Wrong Band - Home", "111", "111")])
        self.assertEqual((rows[0]["artist"], rows[0]["track_id"], rows[0]["match"]), ("Right Band", "222", "hour"))

    def test_a_title_alone_is_the_last_resort_and_no_match_keeps_the_play(self):
        _, rows = self.slim([play("Rare"), play("Unknown")], [("20200101", "1", "Some Band - Rare", "333", "333")])
        self.assertEqual([r["match"] for r in rows], ["title", "none"])
        self.assertEqual(rows[1]["artist"], "")
        self.assertTrue(rows[1]["track_id"].startswith("x-"))


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "t.duckdb"))
        self.addCleanup(self.store.close)
        self.music = M.Music(cfg())
        a = write(os.path.join(self.dir, "a.csv"), ACTIVITY, [play("Old, \"quoted\" song"), play("New", "2030-01-01T00:00:00.000Z")])
        d = write(os.path.join(self.dir, "d.csv"), DAILY, [("20240301", "10", "Band - Old, \"quoted\" song", "444", "444")])
        self.file = os.path.join(self.dir, "out.csv.gz")
        X.slim(a, d, self.file, log=lambda _: None)

    def test_importing_twice_replaces(self):
        self.assertEqual(self.store.music_import(self.file), 2)
        self.assertEqual(self.store.music_import(self.file), 2)
        p = self.store.music_plays(0)[0]
        self.assertEqual((p["name"], p["artist"], p["ms"], p["window_start"]), ('Old, "quoted" song', "Band", 200000, None))

    def test_the_hourly_reads_take_over_from_their_first(self):
        self.music.poll(self.store, NOON, get=answers("a"))
        self.assertEqual(self.store.music_import(self.file), 1)

    def test_the_report_uses_what_was_really_heard(self):
        self.store.music_import(self.file)
        d = self.music.report(self.store, M.ALL, "UTC", 1_900_000_000)
        self.assertEqual(d["totals"]["minutes"], 7)
        self.assertEqual(d["state"]["imported"]["plays"], 2)

    def test_all_means_all_however_old(self):
        self.store.music_import(self.file)
        self.assertEqual(self.music.report(self.store, M.ALL, "UTC", 2_000_000_000)["totals"]["plays"], 2)

    def test_the_catalog_fills_in_genres_once_and_marks_what_it_doesnt_know(self):
        self.store.music_import(self.file)
        got = {}

        def get(url, params, headers, timeout):
            got.update(params)
            return Answer({"data": [{**song("444"), "id": "444"}]})
        self.assertEqual(self.music.enrich(self.store, NOON, get), 1)
        self.assertEqual(got["ids"], "444")
        self.assertEqual(self.music.report(self.store, M.ALL, "UTC", 1_900_000_000)["genres"][0]["label"], "Rock")
        self.assertEqual(self.music.enrich(self.store, NOON, get), 0)


class WebTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        c = from_dict({**CFG, "database": os.path.join(self.dir, "t.duckdb"),
                       "web": {"secure_cookies": False, "allowed_hosts": ["testserver"]}})
        c["apple_music"].update(cfg()["apple_music"], user_token=None)
        store = Store(c["database"])
        store.set_password_hash("owner", hash_password(PASSWORD))
        store.close()
        self.app = create_app(c, FakeFlickr(), lambda: FakeVision(), background=False, scheduler=False)
        self.rt = self.app.state.runtime
        self.c = TestClient(self.app, base_url="http://testserver")
        self.csrf = None

    def login(self):
        self.csrf = self.c.post("/api/auth/login", json={"username": "owner", "password": PASSWORD}).json()["csrf"]

    def post(self, token="t", password=PASSWORD):
        return self.c.post("/api/music/connect", json={"token": token, "password": password}, headers={"X-CSRF-Token": self.csrf})

    def test_all_of_it_is_signed_in_only(self):
        for path in ("/api/music/analytics", "/api/music/state", "/api/music/connect"):
            self.assertEqual(self.c.get(path).status_code, 401, path)

    def test_only_the_connect_page_lets_apple_in(self):
        self.login()
        r = self.c.get("/api/music/connect")
        self.assertEqual(r.status_code, 200)
        self.assertIn("https://js-cdn.music.apple.com", r.headers["content-security-policy"])
        self.assertEqual(r.headers["cross-origin-opener-policy"], "unsafe-none")
        self.assertEqual(r.headers["referrer-policy"], "strict-origin")      # Apple's sign-in fails without it
        self.assertEqual(self.c.get("/").headers["referrer-policy"], "same-origin")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])
        self.assertNotIn("PRIVATE KEY", r.text)
        self.assertNotIn("apple.com", security.CSP)
        self.assertNotIn("apple.com", self.c.get("/").headers["content-security-policy"])

    def test_connecting_needs_the_password_and_a_token_that_reads(self):
        self.login()
        original = M.fetch_recent
        self.addCleanup(setattr, M, "fetch_recent", original)
        M.fetch_recent = lambda *a, **k: []
        self.assertEqual(self.post(password="wrong password").status_code, 403)
        self.assertFalse(self.rt.cfg["apple_music"]["user_token"])

        def refuse(*a, **k):
            raise M.NotAuthorized("no")
        M.fetch_recent = refuse
        self.assertEqual(self.post().status_code, 400)
        self.assertNotIn("apple_music.user_token", self.rt.store().secret_rows())

        M.fetch_recent = lambda *a, **k: []
        self.assertEqual(self.post().status_code, 200)
        self.assertIn("apple_music.user_token", self.rt.store().secret_rows())

    def test_importing_needs_the_password_and_the_right_file(self):
        self.login()
        out = os.path.join(self.dir, "out.csv.gz")
        X.slim(write(os.path.join(self.dir, "a.csv"), ACTIVITY, [play("Song")]), write(os.path.join(self.dir, "d.csv"), DAILY, []), out, log=lambda _: None)
        with open(out, "rb") as f:
            body = f.read()
        send = lambda data, password: self.c.post("/api/music/import", content=data, headers={"X-CSRF-Token": self.csrf, "X-Confirm-Password": password})
        self.assertEqual(send(body, "wrong password").status_code, 403)
        self.assertEqual(send(b"not a gzip", PASSWORD.replace(" ", "%20")).status_code, 400)
        self.assertEqual(send(body, PASSWORD.replace(" ", "%20")).json(), {"plays": 1})
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".csv.gz") and f != "out.csv.gz"], [])

    def test_the_analytics_answer(self):
        self.login()
        d = self.c.get("/api/music/analytics", params={"days": 30, "tz": "America/Chicago"}).json()
        self.assertEqual(d["totals"]["plays"], 0)
        self.assertFalse(d["state"]["connected"])


if __name__ == "__main__":
    unittest.main()
