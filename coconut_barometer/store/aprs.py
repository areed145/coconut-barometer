"""APRS positions heard from APRS-IS for the callsigns in Settings → APRS, metric like the weather.
  aprs_positions   one row per position packet, keyed by (callsign, ts): where, altitude (m), speed (m/s),
                   course, the symbol (table + code), the comment, the path and the raw packet"""
from __future__ import annotations

import time

from .db import Database

TRACK_COLS = "ts, lat, lon, altitude, speed, course"
PACKET_COLS = "callsign, ts, lat, lon, altitude, speed, course, symbol, comment, path, raw"


class AprsStore(Database):
    def aprs_save(self, packet: dict, ts: int | None = None) -> bool:
        """One position parsed by aprslib (speed in km/h). A second packet in the same second is dropped.
        Returns whether it was stored."""
        ts, speed = int(ts or time.time()), packet.get("speed")
        if self.db.execute("SELECT 1 FROM aprs_positions WHERE callsign=? AND ts=?", (packet["from"], ts)).fetchone():
            return False
        self.db.execute("INSERT OR IGNORE INTO aprs_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (packet["from"], ts, packet["latitude"], packet["longitude"],
                         packet.get("altitude"), None if speed is None else speed / 3.6, packet.get("course"),
                         (packet.get("symbol_table") or "") + (packet.get("symbol") or ""),
                         (packet.get("comment") or "").strip() or None, ",".join(packet.get("path") or []),
                         packet.get("raw")))
        return True

    def aprs_latest(self, callsigns: list[str]) -> list[dict]:
        """Each callsign's newest position (None until one is heard), how many are stored, and since when."""
        out = []
        for call in callsigns:
            last = self.db.execute(f"SELECT {PACKET_COLS} FROM aprs_positions WHERE callsign=? ORDER BY ts DESC LIMIT 1",
                                   (call,)).fetchone()
            n, first = self.db.execute("SELECT COUNT(*), MIN(ts) FROM aprs_positions WHERE callsign=?", (call,)).fetchone()
            out.append({"callsign": call, "last": dict(last) if last else None, "positions": n, "first_ts": first})
        return out

    def aprs_track(self, callsign: str, start: int, end: int) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            f"SELECT {TRACK_COLS} FROM aprs_positions WHERE callsign=? AND ts BETWEEN ? AND ? ORDER BY ts",
            (callsign, start, end))]

    def aprs_packets(self, callsign: str, limit: int = 25) -> list[dict]:
        """The newest packets, newest first."""
        return [dict(r) for r in self.db.execute(
            f"SELECT {PACKET_COLS} FROM aprs_positions WHERE callsign=? ORDER BY ts DESC LIMIT ?", (callsign, limit))]
