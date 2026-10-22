"""APRS: callsign settings, what the APRS-IS lines become, storage, the stream thread, and the APRS API."""
import os
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from flickr_organizer.aprs.service import AprsService
from flickr_organizer.aprs.stream import LOGIN, AprsStream, Hub, callsigns, handle
from flickr_organizer.config import from_dict, validate
from flickr_organizer.store import Store
from flickr_organizer.web.app import create_app
from flickr_organizer.web.security import hash_password

NOW = 1_700_000_000
CALL = "KJ4TFD-10"


def pos(call=CALL, lat="3016.20N", lon="09744.40W", rest=">090/025/A=000650 mobile"):
    return f"{call}>APRS,WIDE1-1,qAR,KJ4TFD:!{lat}/{lon}{rest}"


def db():
    return Store(os.path.join(tempfile.mkdtemp(), "db.duckdb"))


class SettingsTest(unittest.TestCase):
    def test_callsigns_are_upper_cased_and_bad_ones_refused(self):
        self.assertEqual(callsigns({"aprs": {"callsigns": [" kj4tfd-10 ", "KJ4TFD-10", "W1AW"]}}), [CALL, "W1AW"])
        errors, _ = validate({"aprs": {"callsigns": ["KJ4TFD-10", "bad call\r\nfilter"]}},
                             from_dict({**CFG, "aprs": {"callsigns": ["KJ4TFD-10", "bad call\r\nfilter"]}}))
        self.assertEqual([e for e in errors if e.startswith("aprs")], ["aprs.callsigns[1]: 'bad call\\r\\nfilter' isn't a callsign "
                                                                       "(like KJ4TFD or KJ4TFD-10)"])

    def test_the_first_callsign_is_tracked_by_default(self):
        self.assertEqual(callsigns(from_dict(CFG)), [CALL])


class HandleTest(unittest.TestCase):
    def setUp(self):
        self.store, self.hub = db(), Hub()

    def test_a_position_is_stored_metric(self):
        p = handle(pos(), self.store, self.hub, [CALL], now=NOW)
        self.assertEqual((p["latitude"], p["longitude"]), (30.27, -97.74))
        (row,) = self.store.aprs_packets(CALL)
        self.assertEqual((row["ts"], row["lat"], row["lon"], row["course"], row["symbol"]), (NOW, 30.27, -97.74, 90, "/>"))
        self.assertAlmostEqual(row["speed"], 25 * 1.852 / 3.6, 2)      # 25 knots, in m/s
        self.assertAlmostEqual(row["altitude"], 198.12, 1)
        self.assertEqual((row["comment"], row["path"]), ("mobile", "WIDE1-1,qAR,KJ4TFD"))
        self.assertEqual((self.hub.status["packets"], self.hub.status["positions"]), (1, 1))

    def test_others_status_keepalives_and_junk_are_not_stored(self):
        for line in [pos(call="W1AW"), f"{CALL}>APRS:>on the road", "# aprsc 2.1 keepalive", "garbage", "",
                     "# logresp KJ4TFD-RO unverified, server T2TEXAS"]:
            self.assertIsNone(handle(line, self.store, self.hub, [CALL], now=NOW))
        self.assertEqual(self.store.aprs_packets(CALL), [])
        self.assertEqual((self.hub.status["server"], self.hub.status["positions"]), ("T2TEXAS", 0))

    def test_the_same_second_twice_is_kept_once(self):
        handle(pos(), self.store, self.hub, [CALL], now=NOW)
        handle(pos(rest=">090/025 via another igate"), self.store, self.hub, [CALL], now=NOW)
        self.assertEqual(len(self.store.aprs_packets(CALL)), 1)
        self.assertEqual(self.hub.status["positions"], 1)


class StoreTest(unittest.TestCase):
    def test_latest_and_track(self):
        store, hub = db(), Hub()
        for i, lat in enumerate(["3016.20N", "3016.80N", "3017.40N"]):
            handle(pos(lat=lat), store, hub, [CALL], now=NOW + 60 * i)
        latest = {c["callsign"]: c for c in store.aprs_latest([CALL, "W1AW"])}
        self.assertEqual((latest[CALL]["positions"], latest[CALL]["first_ts"], latest[CALL]["last"]["ts"]), (3, NOW, NOW + 120))
        self.assertEqual((latest["W1AW"]["positions"], latest["W1AW"]["last"]), (0, None))
        self.assertEqual([p["ts"] for p in store.aprs_track(CALL, NOW + 30, NOW + 200)], [NOW + 60, NOW + 120])


class FakeSocket:
    """Hands out the given chunks (split mid-line, as TCP may), then sets stop and reports the connection closed."""
    def __init__(self, chunks, stop):
        self.chunks, self.stop, self.sent, self.closed = list(chunks), stop, b"", False

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if self.chunks:
            return self.chunks.pop(0)
        self.stop.set()
        return b""

    def close(self):
        self.closed = True


class StreamThreadTest(unittest.TestCase):
    def test_logs_in_with_a_filter_and_stores_positions(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        svc = AprsService(from_dict({**CFG, "database": path, "aprs": {"callsigns": [CALL, "w1aw"]}}), path)
        stop = threading.Event()
        line = (pos() + "\r\n").encode()
        sock = FakeSocket([b"# logresp KJ4TFD-RO unverified, server T2X\r\n" + line[:20], line[20:]], stop)
        t = AprsStream(svc, stop, connect=lambda addr: sock)
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(sock.sent.decode(), LOGIN.format(user="KJ4TFD-RO", calls="KJ4TFD-10/W1AW"))
        self.assertTrue(sock.sent.endswith(b"filter b/KJ4TFD-10/W1AW\r\n"))
        self.assertEqual(len(svc.store().aprs_packets(CALL)), 1)
        # The server closing the connection is a drop: it's retried.
        self.assertEqual((svc.hub.status["server"], svc.hub.status["state"], sock.closed), ("T2X", "reconnecting", True))
        self.assertIn("closed the connection", svc.hub.status["error"])


class AprsApiTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        store = Store(path)
        store.set_password_hash("owner", hash_password("correct horse battery"))
        now, hub = int(time.time()), Hub()
        for i, lat in enumerate(["3016.20N", "3016.80N"]):
            handle(pos(lat=lat), store, hub, [CALL], now=now - 600 + 60 * i)
        handle(pos(), store, hub, [CALL], now=now - 3 * 86400)
        cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]}})
        self.c = TestClient(create_app(cfg, FakeFlickr(), provider_factory=lambda: FakeVision("m"), scheduler=False),
                            base_url="https://testserver")

    def login(self):
        self.c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"})

    def test_needs_login(self):
        self.assertEqual(self.c.get("/api/aprs/stations").status_code, 401)

    def test_stations_track_and_packets(self):
        self.login()
        s = self.c.get("/api/aprs/stations").json()
        self.assertEqual([c["callsign"] for c in s["callsigns"]], [CALL])
        self.assertEqual((s["callsigns"][0]["positions"], s["units"], s["stream"]["state"]), (3, "metric", "off"))
        self.assertEqual(len(self.c.get("/api/aprs/track", params={"callsign": "kj4tfd-10", "hours": 24}).json()["points"]), 2)
        self.assertEqual(len(self.c.get("/api/aprs/track", params={"callsign": CALL, "hours": 24 * 7}).json()["points"]), 3)
        self.assertEqual(len(self.c.get("/api/aprs/packets", params={"callsign": CALL, "limit": 2}).json()["packets"]), 2)
        self.assertEqual(self.c.get("/api/aprs/track", params={"callsign": "W1AW"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
