"""Reports the Tempests to CWOP (the Citizen Weather Observer Program) as APRS weather packets over APRS-IS:
they show on aprs.fi and CWOP's maps, and NOAA's MADIS takes them in.

Settings → APRS → Weather reports: which stations report, as which callsign-SSID, how often, and the APRS-IS
passcode. Each round opens its own logged-in connection, sends one packet per station and closes; the
tracking stream (stream.py) stays receive-only. Nothing is sent until a station is given a callsign, and a
station whose newest reading is older than STALE_AFTER is skipped rather than reported stale.

The packet (APRS 1.0.1, ch. 12, "complete weather report" with a position and timestamp):
    KK6GPV-13>APZCBR,TCPIP*:/231504z3022.40N/09537.17W_090/005g010t072r000p000P000h55b10149L123 WeatherFlow Tempest
wind direction / speed (mph, the 1-minute average), g peak gust over 5 minutes (mph), t °F, r p P rain in
hundredths of an inch over the hour / 24 hours / since local midnight, h humidity (00 = 100 %), b sea-level
pressure in tenths of hPa, L/l sunlight in W/m² (l: 1000 and up). A reading that's missing is dots.
"""
from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timezone

from .. import activity
from ..errors import caught
from .stream import CALLSIGN, SERVER

TOCALL = "APZCBR"          # APZ: experimental software (no registered tocall for this app)
STALE_AFTER = 15 * 60
FIRST_AFTER = 60           # the first round, this long after starting (the stream fills in first)
LOGIN = "user {call} pass {passcode} vers coconut-barometer 1.0\r\n"


def aprs_passcode(callsign: str) -> int:
    """The APRS-IS passcode for a callsign (the SSID doesn't matter): the published hash every server checks."""
    base, h = callsign.split("-")[0].upper(), 0x73E2
    for i in range(0, len(base), 2):
        h ^= ord(base[i]) << 8
        if i + 1 < len(base):
            h ^= ord(base[i + 1])
    return h & 0x7FFF


def _angle(v: float, degree_digits: int, hemis: str) -> str:
    """Degrees as APRS writes them: DDMM.mm (or DDDMM.mm) and the hemisphere."""
    hundredths = round(abs(v) * 6000)                  # minutes to 2 decimals, carried properly at 59.995
    deg, minutes = divmod(hundredths, 6000)
    return f"{deg:0{degree_digits}d}{minutes / 100:05.2f}{hemis[0] if v >= 0 else hemis[1]}"


def _num(v, width: int, scale: float = 1.0, lo: int = 0) -> str:
    """A field of `width` digits (negative allowed down to `lo`), or dots when there's no reading."""
    if v is None:
        return "." * width
    n = max(lo, min(10 ** width - 1, round(v * scale)))
    return f"{n:0{width}d}"


def wx_packet(callsign: str, w: dict) -> str:
    """The APRS weather packet for one report (`w` from WeatherStore.wx_report, metric)."""
    mph, hundredths_in = 2.23694, 100 / 25.4
    stamp = datetime.fromtimestamp(w["ts"], timezone.utc).strftime("%d%H%Mz")
    calm = not w.get("wind_avg")
    temp = None if w.get("temp") is None else w["temp"] * 1.8 + 32
    hum = w.get("humidity")
    solar = w.get("solar")
    body = (f"/{stamp}{_angle(w['lat'], 2, 'NS')}/{_angle(w['lon'], 3, 'EW')}_"
            f"{'000' if calm or w.get('wind_dir') is None else _num(w['wind_dir'] % 360 or 360, 3)}/"
            f"{_num(w.get('wind_avg'), 3, mph)}g{_num(w.get('gust_5min'), 3, mph)}"
            f"t{'...' if temp is None else (f'{round(temp):03d}' if temp >= 0 else f'{max(-99, round(temp)):03d}')}"
            f"r{_num(w.get('rain_1h'), 3, hundredths_in)}p{_num(w.get('rain_24h'), 3, hundredths_in)}"
            f"P{_num(w.get('rain_midnight'), 3, hundredths_in)}"
            f"h{'..' if hum is None else ('00' if round(hum) >= 100 else _num(max(1, hum), 2))}"
            f"b{_num(w.get('pressure_sl'), 5, 10)}"
            + ("" if solar is None else f"L{_num(solar, 3)}" if solar < 1000 else f"l{_num(solar - 1000, 3)}"))
    return f"{callsign}>{TOCALL},TCPIP*:{body} WeatherFlow Tempest"


def one_callsign(v) -> str:
    """A station's callsign as Settings saves it: text, or a list of one (the key → value field's form). "" otherwise."""
    if isinstance(v, (list, tuple)):
        v = v[0] if len(v) == 1 else ""
    return str(v or "").strip().upper()


def configured(cfg: dict, stations: list[dict]) -> list[tuple[dict, str]]:
    """(station, callsign) for each station given one in Settings, matched by name (or device id), ignoring case."""
    wanted = {str(k).strip().lower(): one_callsign(v) for k, v in (cfg["aprs"].get("cwop") or {}).items()}
    out = []
    for s in stations:
        call = wanted.get(str(s["name"]).strip().lower()) or wanted.get(str(s["device_id"]))
        if call and CALLSIGN.match(call):
            out.append((s, call))
    return out


class CwopReporter(threading.Thread):
    """Every aprs.cwop_minutes: one connection, one packet per configured station with a fresh reading."""

    def __init__(self, svc, stop: threading.Event, connect=None, clock=time.time):
        super().__init__(name="cwop", daemon=True)
        self.svc, self.stop, self._connect, self._clock = svc, stop, connect, clock
        self.last: dict = {"at": None, "sent": [], "error": None}

    def run(self) -> None:
        wait = FIRST_AFTER
        while not self.stop.wait(wait):
            try:
                self.report()
            except Exception as e:  # noqa: BLE001 - a failed round is logged; the next one tries again
                caught(e, "cwop")
                self.last = {"at": int(self._clock()), "sent": [], "error": str(e)[:200]}
            wait = max(5, int(self.svc.cfg["aprs"].get("cwop_minutes") or 10)) * 60

    def packets(self) -> list[str]:
        db, now, out = self.svc.store(), self._clock(), []
        for station, call in configured(self.svc.cfg, db.wx_stations()):
            w = db.wx_report(station["device_id"], int(now))
            if w and w["lat"] is not None and now - w["ts"] <= STALE_AFTER:
                out.append(wx_packet(call, w))
        return out

    def report(self) -> list[str]:
        """One round. Returns the packets sent (none: nothing configured, or nothing fresh to report)."""
        packets = self.packets()
        if not packets:
            return []
        login = packets[0].split(">")[0]
        code = str(self.svc.cfg["aprs"].get("passcode") or "").strip()
        if not code.isdigit() or int(code) != aprs_passcode(login):
            raise ValueError(f"the APRS-IS passcode in Settings → APRS isn't the one for {login.split('-')[0]}")
        with activity.log.call("cwop", "weather report", f"{len(packets)} station{'s' if len(packets) > 1 else ''}") as a:
            conn = (self._connect or (lambda addr: socket.create_connection(addr, timeout=30)))(SERVER)
            try:
                f = conn.makefile("rb")
                f.readline()                                   # the server's banner
                conn.sendall(LOGIN.format(call=login, passcode=code).encode())
                reply = f.readline().decode("ascii", "replace")
                if " verified" not in reply:
                    raise ConnectionError(f"APRS-IS didn't verify {login}: {reply.strip()[:120]}")
                conn.sendall("".join(p + "\r\n" for p in packets).encode())
            finally:
                conn.close()
            a["detail"] = " · ".join(p.split(">")[0] for p in packets)
        self.last = {"at": int(self._clock()), "sent": packets, "error": None}
        return packets
