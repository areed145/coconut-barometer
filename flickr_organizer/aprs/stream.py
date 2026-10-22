"""APRS-IS: a read-only login (passcode -1, so it can't transmit) with a budlist filter, so the server sends
only packets from the tracked callsigns. The servers refuse N0CALL and the like, so the login is the first
tracked callsign with the SSID -RO (receive-only), which no station transmits as. Position packets are stored; the rest
(status, messages, telemetry) are counted and dropped. The server sends a "#" keepalive every ~20 s,
so two quiet minutes means the connection is gone. A change to the callsign list reconnects."""
from __future__ import annotations

import re
import socket
import threading
import time

import aprslib

from .. import activity

SERVER = ("rotate.aprs2.net", 14580)    # the APRS-IS server pool; 14580 is the port that takes a filter
LOGIN = "user {user} pass -1 vers coconut-barometer 1.0 filter b/{calls}\r\n"
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


class Hub:
    """The stream's state, for the APRS page."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = {"state": "off", "since": None, "last_message": None, "packets": 0, "positions": 0,
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


def handle(line: str, store, hub: Hub, tracked: list[str], now: float | None = None) -> dict | None:
    """One line from the server. Returns the position stored, if it was one."""
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
        activity.log.record("aprs", "unreadable packet", line[:200], type(e).__name__, error=True)
        return None
    call = p.get("from")
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
            calls = self.service.callsigns()
            if not calls:
                self.service.hub.update(state="waiting", error="no callsigns")
                self.stop_event.wait(10)
                continue
            try:
                self._session(calls)
                delay = 5
            except Exception as e:
                self.service.hub.update(state="reconnecting", error=f"{type(e).__name__}: {e}"[:200])
                activity.log.record("aprs", "disconnected", f"{type(e).__name__}: {e}"[:200], "reconnecting",
                                    error=True, kind="connect")
                self.service.hub.bump("reconnects")
            self.stop_event.wait(delay)
            delay = min(delay * 2, 300)

    def _session(self, calls: list[str]) -> None:
        hub, store = self.service.hub, self.service.store()
        conn = (self._connect or (lambda a: socket.create_connection(a, timeout=30)))(SERVER)
        try:
            conn.settimeout(QUIET)
            conn.sendall(LOGIN.format(user=calls[0].split("-")[0] + "-RO", calls="/".join(calls)).encode())
            hub.update(state="connected", since=int(time.time()), error="")
            activity.log.record("aprs", "connected", f"{SERVER[0]} · filter b/{'/'.join(calls)}", kind="connect")
            buf = b""
            while not self.stop_event.is_set() and self.service.callsigns() == calls:
                chunk = conn.recv(4096)      # a timeout here (no keepalive) ends the session: reconnect
                if not chunk:
                    raise ConnectionError("the server closed the connection")
                *lines, buf = (buf + chunk).split(b"\n")
                for raw in lines:
                    handle(raw.decode("utf-8", "replace").rstrip("\r"), store, hub, calls)
        finally:
            conn.close()
            hub.update(state="disconnected")
