"""APRS-IS: a read-only login (passcode -1, so it can't transmit) with a budlist filter, so the server sends
only packets from the tracked callsigns, plus an entry-station filter (e/) for the igate: every packet it gated.
The servers refuse N0CALL and the like, so the login is the first tracked callsign (or the igate) with the SSID
-RO (receive-only), which no station transmits as. Tracked positions are stored; the igate's packets go to its
own table (igate.py), and its own beacons say where it is. The rest (status, messages, telemetry from tracked
callsigns) are counted and dropped. The server sends a "#" keepalive every ~20 s, so two quiet minutes means
the connection is gone. A change to the callsigns or the igate reconnects."""
from __future__ import annotations

import re
import socket
import threading
import time

import warnings

with warnings.catch_warnings():     # aprslib 0.7 has regexes with invalid escapes: a SyntaxWarning when first compiled
    warnings.simplefilter("ignore", SyntaxWarning)
    import aprslib

from .. import activity
from ..errors import caught
from .igate import gated_by, header_path

SERVER = ("rotate.aprs2.net", 14580)    # the APRS-IS server pool; 14580 is the port that takes a filter
LOGIN = "user {user} pass -1 vers coconut-barometer 1.0 filter {filter}\r\n"
CALLSIGN = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,2})?$")     # also keeps anything odd out of the login line
QUIET = 120


def callsigns(cfg: dict) -> list[str]:
    """The tracked callsigns, upper-cased, in the order listed (repeats and malformed ones dropped)."""
    out = []
    for c in cfg["aprs"]["callsigns"]:
        c = str(c).strip().upper()
        if CALLSIGN.match(c) and c not in out:
            out.append(c)
    return out


def igate(cfg: dict) -> str:
    """Settings → APRS → Igate, upper-cased; "" when off or malformed."""
    c = str(cfg["aprs"].get("igate") or "").strip().upper()
    return c if CALLSIGN.match(c) else ""


def server_filter(calls: list[str], gate: str) -> str:
    """b/ the tracked callsigns and the igate (its beacons place it), e/ the igate (what it gated)."""
    budlist = calls + ([gate] if gate and gate not in calls else [])
    return " ".join(([f"b/{'/'.join(budlist)}"] if budlist else []) + ([f"e/{gate}"] if gate else []))


class Hub:
    """The stream's state, for the APRS page."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = {"state": "off", "since": None, "last_message": None, "packets": 0, "positions": 0, "gated": 0,
                       "reconnects": 0, "server": "", "error": ""}

    def update(self, **kw) -> None:
        with self.lock:
            self.status.update(kw)

    def bump(self, key: str) -> None:
        with self.lock:
            self.status[key] += 1

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.status)


def handle(line: str, store, hub: Hub, tracked: list[str], now: float | None = None, gate: str = "") -> dict | None:
    """One line from the server. Returns the tracked position stored, if it was one."""
    if not line:
        return None
    if line.startswith("#"):
        if line.startswith("# logresp"):
            hub.update(server=line.rsplit(" ", 1)[-1])
            activity.log.record("aprs", "login", line[2:], "verified" if " verified" in line else "receive-only", kind="connect")
        else:
            activity.log.skip("aprs")           # the server's keepalive, every ~20 s
        return None
    hub.bump("packets")
    hub.update(last_message=int(now or time.time()))
    try:
        p = aprslib.parse(line)
    except (aprslib.ParseError, aprslib.UnknownFormat) as e:
        if gate and (gated_by(header_path(line)) or ("",))[0] == gate:       # still credited, just with no position
            store.igate_save(gate, {"from": line.split(">", 1)[0], "path": header_path(line), "raw": line}, True, now)
            hub.bump("gated")
            return None
        activity.log.record("aprs", "unreadable packet", line[:200], type(e).__name__, error=True)
        return None
    call = p.get("from")
    if gate:
        g = gated_by(p.get("path") or [])
        if g and g[0] == gate:
            store.igate_save(gate, p, g[1], now)
            hub.bump("gated")
        if call == gate and p.get("latitude") is not None:
            store.igate_place(gate, p["latitude"], p["longitude"], now)
        if call not in tracked:
            return None             # the igate's traffic: counted, not listed one by one in Activity
    if call not in tracked or p.get("latitude") is None or p.get("longitude") is None:
        activity.log.record("aprs", p.get("format") or "packet", f"{call} · {line.split(':', 1)[-1][:120]}",
                            "not a tracked position")
        return None
    stored = store.aprs_save(p, now)
    if stored:
        hub.bump("positions")
    speed = f" · {round(p['speed'])} km/h" if p.get("speed") else ""
    activity.log.record("aprs", "position", f"{call} · {p['latitude']:.4f}, {p['longitude']:.4f}{speed}",
                        "stored" if stored else "duplicate")
    return p


class AprsStream(threading.Thread):
    """Runs until stop is set. `service` supplies the callsigns, the hub and the Store to write with."""

    def __init__(self, service, stop: threading.Event, connect=None):
        super().__init__(name="aprs-stream", daemon=True)
        self.service, self.stop_event = service, stop
        self._connect = connect          # tests: a function (address) -> a socket-like object

    def run(self) -> None:
        delay = 5
        while not self.stop_event.is_set():
            calls, gate = self.service.callsigns(), self.service.igate()
            if not calls and not gate:
                self.service.hub.update(state="waiting", error="no callsigns")
                self.stop_event.wait(10)
                continue
            try:
                self._session(calls, gate)
                delay = 5
            except Exception as e:
                caught(e, "aprs")
                self.service.hub.update(state="reconnecting", error=f"{type(e).__name__}: {e}"[:200])
                activity.log.record("aprs", "disconnected", f"{type(e).__name__}: {e}"[:200], "reconnecting",
                                    error=True, kind="connect")
                self.service.hub.bump("reconnects")
            self.stop_event.wait(delay)
            delay = min(delay * 2, 300)

    def _session(self, calls: list[str], gate: str) -> None:
        hub, store = self.service.hub, self.service.store()
        conn = (self._connect or (lambda a: socket.create_connection(a, timeout=30)))(SERVER)
        try:
            conn.settimeout(QUIET)
            wanted = server_filter(calls, gate)
            conn.sendall(LOGIN.format(user=(calls or [gate])[0].split("-")[0] + "-RO", filter=wanted).encode())
            hub.update(state="connected", since=int(time.time()), error="")
            activity.log.record("aprs", "connected", f"{SERVER[0]} · filter {wanted}", kind="connect")
            buf = b""
            while not self.stop_event.is_set() and (self.service.callsigns(), self.service.igate()) == (calls, gate):
                chunk = conn.recv(4096)      # a timeout here (no keepalive) ends the session: reconnect
                if not chunk:
                    raise ConnectionError("the server closed the connection")
                *lines, buf = (buf + chunk).split(b"\n")
                for raw in lines:
                    handle(raw.decode("utf-8", "replace").rstrip("\r"), store, hub, calls, gate=gate)
        finally:
            conn.close()
            hub.update(state="disconnected")
