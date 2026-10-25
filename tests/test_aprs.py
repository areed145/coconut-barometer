"""APRS: callsign settings, what the APRS-IS lines become, storage, the stream thread, and the APRS API."""
import math
import os
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient

from fakes import FakeFlickr, FakeVision, CFG
from coconut_barometer.aprs.service import AprsService
from coconut_barometer.aprs.igate import bin_width, details, gated_by, grid_center, parse_location, station_type, weibull_fit
from coconut_barometer.aprs.stream import LOGIN, AprsStream, Hub, callsigns, handle, server_filter
from coconut_barometer.config import from_dict, validate
from coconut_barometer.store import SCHEMA_VERSION, Store
from coconut_barometer.web.app import create_app
from coconut_barometer.web.security import hash_password

NOW = 1_700_000_000
CALL = "KJ4TFD-10"
GATE = "WA5DTR-15"


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
        self.assertEqual(sock.sent.decode(), LOGIN.format(user="KJ4TFD-RO", filter=f"b/KJ4TFD-10/W1AW/{GATE} e/{GATE}"))
        self.assertTrue(sock.sent.endswith(f"filter b/KJ4TFD-10/W1AW/{GATE} e/{GATE}\r\n".encode()))
        self.assertEqual(len(svc.store().aprs_packets(CALL)), 1)
        # The server closing the connection is a drop: it's retried.
        self.assertEqual((svc.hub.status["server"], svc.hub.status["state"], sock.closed), ("T2X", "reconnecting", True))
        self.assertIn("closed the connection", svc.hub.status["error"])


def gated(call="N5XYZ-9", lat="3016.20N", lon="09744.40W", via="WIDE1-1", gate=GATE, info=None):
    return f"{call}>APRS,{via},qAR,{gate}:" + (info or f"!{lat}/{lon}>")


class IgateTest(unittest.TestCase):
    def setUp(self):
        self.store, self.hub = db(), Hub()

    def handle(self, line, now=NOW):
        return handle(line, self.store, self.hub, [CALL], now=now, gate=GATE)

    def test_the_filter_asks_for_what_it_gated_and_its_beacons(self):
        self.assertEqual(server_filter([CALL], GATE), f"b/{CALL}/{GATE} e/{GATE}")
        self.assertEqual(server_filter([], GATE), f"b/{GATE} e/{GATE}")
        self.assertEqual(server_filter([CALL], ""), f"b/{CALL}")

    def test_the_path_says_who_gated_it_and_whether_direct(self):
        self.assertEqual(gated_by(["WIDE1-1", "WIDE2-1", "qAR", GATE]), (GATE, True))
        self.assertEqual(gated_by(["KJ4TFD-3*", "WIDE2-1", "qAo", GATE]), (GATE, False))
        self.assertIsNone(gated_by(["TCPIP*", "qAC", "T2TEXAS"]))

    def test_gated_packets_are_counted_with_where_they_were(self):
        self.handle(gated())                                              # direct
        self.handle(gated(via="KJ4TFD-3*,WIDE2-1"), now=NOW + 1)         # digipeated
        self.handle(gated(info=">just a status"), now=NOW + 2)           # no position
        self.handle(gated(info=";OBJ      *111111z3016.20N/09744.40W>obj"), now=NOW + 3)   # an object: placed elsewhere
        self.handle(gated(gate="W5OTHER-10"), now=NOW + 4)               # someone else's
        self.handle(f"{GATE}>APRS,TCPIP*,qAC,T2TEXAS:!3010.00N/09740.00W&igate", now=NOW + 5)
        t = self.store.igate_totals(GATE, NOW, NOW + 10)
        self.assertEqual((t["packets"], t["positions"], t["direct"], t["stations"]), (4, 2, 1, 1))
        self.assertEqual(self.hub.status["gated"], 4)
        at = self.store.igate_placed(GATE)
        self.assertEqual((round(at["lat"], 4), round(at["lon"], 4), at["ts"]), (30.1667, -97.6667, NOW + 5))
        self.assertEqual(self.store.aprs_packets(CALL), [])

    def test_location_setting(self):
        self.assertIsNone(parse_location(" "))
        self.assertEqual(parse_location("30.27, -97.74"), (30.27, -97.74))
        lat, lon = grid_center("EM10dg")
        self.assertTrue(30.25 < lat < 30.29 and -97.75 < lon < -97.66)
        self.assertEqual(parse_location("em10"), (30.5, -97.0))
        for bad in ("91, 0", "nowhere", "EM1"):
            self.assertRaises(ValueError, parse_location, bad)
        errors, _ = validate({"aprs": {"igate": "no good", "igate_location": "nowhere"}},
                             from_dict({**CFG, "aprs": {"igate": "no good", "igate_location": "nowhere"}}))
        self.assertEqual(len([e for e in errors if e.startswith("aprs.igate")]), 2)

    def test_station_types_from_the_symbol(self):
        cases = {"/>": "mobile", "/k": "mobile", "\\>": "mobile", "/-": "home", "\\-": "home", "/y": "home",
                 "/_": "weather", "/#": "digi", "S#": "digi", "/&": "igate", "I&": "igate", "/O": "balloon",
                 "\\O": "balloon", "/r": "other", "\\a": "other", None: "other"}
        self.assertEqual({s: station_type(s) for s in cases}, cases)
        self.assertEqual((station_type(None, weather=True), station_type("/>", objects_only=True)), ("weather", "object"))

    def test_details_kept_for_each_gated_packet(self):
        self.handle(gated(info="!3016.20N/09744.40WO/A=040000 pico"))
        self.handle(f"N5XYZ-9>T2SP0W,WIDE1-1,qAR,{GATE}:`|aEl!Pk/`\"4V}}_%", now=NOW + 1)     # Mic-E: no tocall
        self.handle(gated(info=">just a status"), now=NOW + 2)
        rows = self.store.db.execute("SELECT altitude, symbol, comment, tocall FROM aprs_igate ORDER BY ts").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(12192.0, "/O", "pico", "APRS"), (63.0, "/k", "`_%", None),
                                                    (None, None, "just a status", "APRS")])
        self.assertIsNone(details({"altitude": 999_999})["altitude"])

    def test_the_v15_migration_reads_old_rows_again(self):
        self.handle(gated(info="!3016.20N/09744.40W>/A=000500 car"))
        self.store.db.execute("UPDATE aprs_igate SET altitude = NULL, symbol = NULL, comment = NULL, tocall = NULL")
        self.store.set_meta("schema_version", 14)
        again = Store(self.store.path)
        row = again.db.execute("SELECT altitude, symbol, comment, tocall FROM aprs_igate").fetchone()
        self.assertEqual((round(row[0]), row[1], row[2], row[3]), (152, "/>", "car", "APRS"))
        self.assertEqual(int(again.get_meta("schema_version")), SCHEMA_VERSION)

    def test_stations_heard_new_regular_and_by_type(self):
        day = 86400
        for d in range(10):                                          # a regular, heard every day for ten days
            self.handle(gated(call="N5REG-9"), now=NOW - d * day)
        self.handle(gated(call="N5OLD-1", info="!3016.20N/09744.40W-home"), now=NOW - 20 * day)   # heard before the window
        self.handle(gated(call="N5OLD-1", info="!3016.20N/09744.40W-home"), now=NOW)
        self.handle(gated(call="N5WX", info="_10090556c220s004g005t077r000p000P000h50b09900wRSW"), now=NOW)
        self.handle(gated(call="N5OBJ", info=";OBJ      *111111z3016.20N/09744.40W>obj"), now=NOW)
        s = self.store.igate_stations((30.0, -97.0), GATE, NOW - 2 * day, NOW)
        by = {x["callsign"]: x for x in s["stations"]}
        self.assertEqual(list(by), ["N5REG-9", "N5OBJ", "N5OLD-1", "N5WX"])
        self.assertEqual({c: (x["type"], x["new"], x["regular"]) for c, x in by.items()},
                         {"N5REG-9": ("mobile", False, True), "N5OLD-1": ("home", False, False),
                          "N5WX": ("weather", True, False), "N5OBJ": ("object", True, False)})
        self.assertEqual((s["count"], s["new"], s["regular"], s["types"]["mobile"], s["types"]["other"]), (4, 2, 1, 1, 0))
        self.assertAlmostEqual(by["N5REG-9"]["km"], 77, delta=1)      # 30.27N 97.74W from 30N 97W
        self.assertIsNone(by["N5WX"]["km"])                            # no position, no distance
        self.assertEqual(s["bucket"], day)
        self.assertEqual(sum(n for _, n, _new in s["days"]), 2 + 4)     # NOW - 2 days: N5REG; NOW - 1: N5REG; NOW: all 4
        self.assertEqual(sum(new for *_, new in s["days"]), 2)
        self.assertIsNone(self.store.igate_stations(None, GATE, NOW - day, NOW)["stations"][0]["km"])

    def test_weibull_fit_recovers_its_range(self):
        # A Weibull with k=2, λ=30 km, binned at 2 km: its 90% distance is 30·ln(10)^(1/2) ≈ 45.5 km.
        k, lam, total, width = 2.0, 30.0, 100_000, bin_width(70)
        cdf = lambda r: 1 - math.exp(-((r / lam) ** k))
        counts = [round(total * (cdf((i + 1) * width) - cdf(i * width))) for i in range(int(70 / width))]
        fit = weibull_fit(width, counts, total)
        self.assertEqual(width, 5)
        self.assertAlmostEqual(fit["k"], 2, delta=0.1)
        self.assertAlmostEqual(fit["effective_km"], 45.5, delta=1.5)
        self.assertGreater(fit["r2"], 0.99)
        self.assertAlmostEqual(sum(fit["curve"]), sum(counts), delta=total * 0.01)
        self.assertIsNone(weibull_fit(5, [10], 10))


class AprsApiTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        store = Store(path)
        store.set_password_hash("owner", hash_password("correct horse battery"))
        now, hub = int(time.time()), Hub()
        for i, lat in enumerate(["3016.20N", "3016.80N"]):
            handle(pos(lat=lat), store, hub, [CALL], now=now - 600 + 60 * i)
        handle(pos(), store, hub, [CALL], now=now - 3 * 86400)
        # The igate at 30.0N 97.0W; stations about 11, 22 and 33 km north of it, and one digipeated from 111 km.
        for i, (lat, via) in enumerate([("3006.00N", "WIDE1-1"), ("3012.00N", "WIDE1-1"), ("3018.00N", "WIDE2-1"),
                                        ("3100.00N", "KJ4TFD-3*")]):
            handle(gated(call=f"N5XY{i}", lat=lat, lon="09700.00W", via=via), store, hub, [CALL], now=now - 60 + i, gate=GATE)
        handle(f"{GATE}>APRS,TCPIP*,qAC,T2TEXAS:!3000.00N/09700.00W&igate", store, hub, [CALL], now=now, gate=GATE)
        cfg = from_dict({**CFG, "database": path, "web": {"allowed_hosts": ["testserver"]}})
        self.c = TestClient(create_app(cfg, FakeFlickr(), provider_factory=lambda: FakeVision("m"), scheduler=False),
                            base_url="https://testserver")

    def login(self):
        self.c.post("/api/auth/login", json={"username": "owner", "password": "correct horse battery"})

    def test_public(self):
        self.assertEqual(self.c.get("/api/aprs/stations").status_code, 200)

    def test_stations_track_and_packets(self):
        self.login()
        s = self.c.get("/api/aprs/stations").json()
        self.assertEqual([c["callsign"] for c in s["callsigns"]], [CALL])
        self.assertEqual((s["callsigns"][0]["positions"], s["units"], s["stream"]["state"]), (3, "metric", "off"))
        self.assertEqual(len(self.c.get("/api/aprs/track", params={"callsign": "kj4tfd-10", "hours": 24}).json()["points"]), 2)
        self.assertEqual(len(self.c.get("/api/aprs/track", params={"callsign": CALL, "hours": 24 * 7}).json()["points"]), 3)
        self.assertEqual(len(self.c.get("/api/aprs/packets", params={"callsign": CALL, "limit": 2}).json()["packets"]), 2)
        self.assertEqual(self.c.get("/api/aprs/track", params={"callsign": "W1AW"}).status_code, 404)

    def test_igate(self):
        g = self.c.get("/api/aprs/igate", params={"hours": 24, "cell": "0.1"}).json()
        self.assertEqual((g["igate"], g["home"]["source"], g["totals"]["packets"], g["totals"]["direct"]), (GATE, "beacon", 4, 3))
        self.assertEqual(sorted(n for *_, n, _s in g["cells"]["rows"]), [1, 1, 1])
        d = g["distance"]
        self.assertEqual((d["n"], round(d["max_km"])), (3, 33))
        self.assertEqual(sum(d["counts"]) + d["beyond"], 3)
        self.assertEqual([f["callsign"] for f in g["farthest"]], ["N5XY2", "N5XY1", "N5XY0"])
        everything = self.c.get("/api/aprs/igate", params={"hours": 24, "direct": "false", "count": "stations"}).json()
        self.assertEqual((everything["distance"]["n"], round(everything["distance"]["max_km"])), (4, 111))
        self.assertEqual(self.c.get("/api/aprs/igate", params={"cell": "bogus"}).status_code, 422)

    def test_igate_stations(self):
        s = self.c.get("/api/aprs/igate/stations", params={"hours": 24}).json()
        self.assertEqual((s["igate"], s["count"], s["new"]), (GATE, 4, 4))
        self.assertEqual({x["type"] for x in s["stations"]}, {"mobile"})


if __name__ == "__main__":
    unittest.main()
