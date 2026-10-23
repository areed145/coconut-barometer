"""CWOP weather reports over APRS-IS: the packet, the passcode, and one round against a fake server."""
import os
import tempfile
import threading
import unittest

from fakes import CFG
from coconut_barometer.aprs.cwop import CwopReporter, aprs_passcode, configured, wx_packet
from coconut_barometer.config import from_dict, validate
from coconut_barometer.store import Store

TS = 1792770300                      # 2026-10-23 15:45 UTC
STATION = {"station_id": 7, "name": "Coconut Barometer", "latitude": 30.37339, "longitude": -95.61948,
           "timezone": "America/Chicago", "station_meta": {"elevation": 0.0},
           "devices": [{"device_id": 11, "device_type": "ST", "serial_number": "ST-1"}]}


def report(**over):
    w = dict(ts=TS, lat=30.37339, lon=-95.61948, wind_dir=90, wind_avg=2.2352, gust_5min=4.4704, temp=22.2222,
             rain_1h=0, rain_24h=2.54, rain_midnight=0.254, humidity=55, pressure_sl=1014.9, solar=123)
    return {**w, **over}


class PacketTest(unittest.TestCase):
    def test_a_complete_report(self):
        self.assertEqual(wx_packet("KK6GPV-13", report()),
                         "KK6GPV-13>APZCBR,TCPIP*:/231545z3022.40N/09537.17W_090/005g010t072r000p010P001h55b10149L123 "
                         "WeatherFlow Tempest")

    def test_edge_values(self):
        body = lambda **o: wx_packet("KK6GPV-13", report(**o)).split(":", 1)[1].split(" ")[0]
        self.assertIn("_000/000g000", body(wind_avg=0, gust_5min=0, wind_dir=270))      # calm
        self.assertIn("_360/", body(wind_dir=0))                                         # north is 360, 000 is calm
        self.assertIn("t-04", body(temp=-20))                                            # -4 °F
        self.assertIn("h00", body(humidity=100))
        self.assertIn("l250", body(solar=1250))                                          # 1000 W/m² and up
        missing = body(temp=None, humidity=None, pressure_sl=None, solar=None, gust_5min=None)
        self.assertIn("g...t...", missing)
        self.assertIn("h..b.....", missing)
        self.assertTrue(missing.endswith("b....."))
        self.assertIn("4500.00S/00000.00E", body(lat=-45, lon=0))
        self.assertIn("3100.00N", body(lat=30.999999))                                   # minutes carried, not 60.00

    def test_passcode(self):
        self.assertEqual(aprs_passcode("N0CALL"), 13023)                                 # the published reference
        self.assertEqual(aprs_passcode("kk6gpv-13"), aprs_passcode("KK6GPV"))           # any SSID, any case

    def test_settings_are_checked(self):
        raw = {"aprs": {"cwop": {"Coconut Barometer": "not a call"}, "passcode": "12a"}}
        errors, _ = validate(raw, from_dict({**CFG, **raw}))
        joined = "\n".join(errors)
        self.assertIn("aprs.cwop.Coconut Barometer: 'not a call' isn't one callsign-SSID", joined)
        self.assertIn("aprs.passcode: an APRS-IS passcode is a number", joined)
        # The Settings page's key → value field saves a value as a list of one: that's fine; two aren't.
        ok = {"aprs": {"cwop": {"Coconut Barometer": ["KK6GPV-14"], "Hewie Barometer": "KK6GPV-13"}}}
        self.assertEqual([e for e in validate(ok, from_dict({**CFG, **ok}))[0] if "cwop" in e], [])
        same = {"aprs": {"cwop": {"Coconut Barometer": "KK6GPV-13", "Hewie Barometer": "kk6gpv-13"}}}
        self.assertIn("each station needs its own SSID", "\n".join(validate(same, from_dict({**CFG, **same}))[0]))
        two = {"aprs": {"cwop": {"Coconut Barometer": ["KK6GPV-14", "KK6GPV-15"]}}}
        self.assertTrue([e for e in validate(two, from_dict({**CFG, **two}))[0] if "cwop" in e])


class FakeServer:
    def __init__(self, reply="# logresp KK6GPV-13 verified, server T2TEST\r\n"):
        self.reply, self.sent, self.closed = reply, b"", False

    def __call__(self, addr):
        self.addr = addr
        return self

    def makefile(self, mode):
        import io
        return io.BytesIO(b"# aprsc 2.1.14\r\n" + self.reply.encode())

    def sendall(self, data):
        self.sent += data

    def close(self):
        self.closed = True


class Svc:
    def __init__(self, path, cfg):
        self.cfg, self._path = cfg, path

    def store(self):
        return Store(self._path, migrate=False)


class ReporterTest(unittest.TestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), "db.duckdb")
        db = Store(path)
        db.wx_save_stations([STATION])
        db.wx_save_obs(11, [[TS, 1.0, 2.2352, 4.4704, 90.0, 3, 1014.9, 22.2222, 55.0, 10000, 2.0, 123.0, 0.0, 0, 0, 0, 2.7, 1]])
        db.close()
        self.cfg = from_dict({**CFG, "aprs": {"cwop": {"coconut barometer": "kk6gpv-13"}, "passcode": str(aprs_passcode("KK6GPV"))}})
        self.svc = Svc(path, self.cfg)

    def reporter(self, server, now=TS + 60):
        return CwopReporter(self.svc, threading.Event(), connect=server, clock=lambda: now)

    def test_a_round_logs_in_and_sends_one_packet_per_station(self):
        server = FakeServer()
        sent = self.reporter(server).report()
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("KK6GPV-13>APZCBR,TCPIP*:/231545z3022.40N/09537.17W_090/005g010t072"))
        lines = server.sent.decode().split("\r\n")
        self.assertEqual(lines[0], f"user KK6GPV-13 pass {aprs_passcode('KK6GPV')} vers coconut-barometer 1.0")
        self.assertEqual(lines[1], sent[0])
        self.assertTrue(server.closed)

    def test_nothing_is_sent_unless_configured_fresh_and_verified(self):
        self.cfg["aprs"]["cwop"] = {}
        self.assertEqual(self.reporter(FakeServer()).report(), [])                   # no station given a callsign
        self.cfg["aprs"]["cwop"] = {"Coconut Barometer": "KK6GPV-13"}
        self.assertEqual(self.reporter(FakeServer(), now=TS + 16 * 60).report(), [])   # reading too old
        self.cfg["aprs"]["passcode"] = "12345"
        with self.assertRaisesRegex(ValueError, "isn't the one for KK6GPV"):
            self.reporter(FakeServer()).report()
        self.cfg["aprs"]["passcode"] = str(aprs_passcode("KK6GPV"))
        server = FakeServer(reply="# logresp KK6GPV-13 unverified, server T2TEST\r\n")
        with self.assertRaisesRegex(ConnectionError, "didn't verify"):
            self.reporter(server).report()
        self.assertEqual(server.sent.count(b"APZCBR"), 0)                          # no packet after a failed login

    def test_stations_match_by_name_or_device_id(self):
        stations = [{"name": "Coconut Barometer", "device_id": 11}, {"name": "Hewie Barometer", "device_id": 22}]
        cfg = {"aprs": {"cwop": {"COCONUT barometer ": ["kk6gpv-13"], "22": "KK6GPV-14", "Nowhere": "KK6GPV-15"}}}
        self.assertEqual([(s["device_id"], c) for s, c in configured(cfg, stations)], [(11, "KK6GPV-13"), (22, "KK6GPV-14")])


if __name__ == "__main__":
    unittest.main()
