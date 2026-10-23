"""APRS positions heard from APRS-IS for the callsigns in Settings → APRS, metric like the weather.
  aprs_positions   one row per position packet, keyed by (callsign, ts): where, altitude (m), speed (m/s),
                   course, the symbol (table + code), the comment, the path and the raw packet
  aprs_igate       one row per packet the igate gated: the sender, where it said it was (objects and items,
                   which place something else, and packets with no position have none), whether it was heard
                   direct, the path and the raw packet. The igate's own last position is meta aprs_igate_at."""
from __future__ import annotations

import json
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

    # ---- the igate ----------------------------------------------------------------------------------------
    def igate_save(self, igate: str, packet: dict, direct: bool, ts: int | None = None) -> None:
        placed = packet.get("latitude") is not None and packet.get("format") not in ("object", "item") \
            and (packet["latitude"], packet["longitude"]) != (0, 0)
        self.db.execute("INSERT INTO aprs_igate VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (igate, packet.get("from"), int(ts or time.time()), packet["latitude"] if placed else None,
                         packet["longitude"] if placed else None, direct, packet.get("format"),
                         ",".join(packet.get("path") or []), packet.get("raw")))

    def igate_place(self, igate: str, lat: float, lon: float, ts: int | None = None) -> None:
        """Where the igate's own beacon says it is."""
        self.set_meta("aprs_igate_at", json.dumps({"callsign": igate, "lat": lat, "lon": lon, "ts": int(ts or time.time())}))

    def igate_placed(self, igate: str) -> dict | None:
        at = json.loads(self.get_meta("aprs_igate_at", "null"))
        return at if at and at["callsign"] == igate else None

    def igate_totals(self, igate: str, start: int, end: int) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) AS packets, COUNT(lat) AS positions, COUNT(lat) FILTER (WHERE direct) AS direct, "
            "COUNT(DISTINCT callsign) AS stations, COUNT(DISTINCT callsign) FILTER (WHERE direct) AS direct_stations "
            "FROM aprs_igate WHERE igate=? AND ts BETWEEN ? AND ?", (igate, start, end)).fetchone()
        first = self.db.execute("SELECT MIN(ts) FROM aprs_igate WHERE igate=?", (igate,)).fetchone()[0]
        return {**dict(row), "first_ts": first}

    def igate_cells(self, igate: str, start: int, end: int, dlat: float, dlon: float, direct: bool) -> list[dict]:
        """Packets with a position, counted by grid cell; each cell by its south-west corner."""
        return [dict(r) for r in self.db.execute(
            "SELECT floor(lat / ?) * ? AS lat, floor(lon / ?) * ? AS lon, COUNT(*) AS n, COUNT(DISTINCT callsign) AS stations "
            "FROM aprs_igate WHERE igate=? AND ts BETWEEN ? AND ? AND lat IS NOT NULL AND (direct OR NOT ?) "
            "GROUP BY 1, 2 ORDER BY n DESC", (dlat, dlat, dlon, dlon, igate, start, end, direct))]

    def _igate_distances(self, per_station: bool) -> str:
        """The distances (km) from the igate (?, ?, ? = lat, lat, lon) of the window's packets, or with
        per_station, each station's farthest. Takes igate, start, end, direct after the three."""
        km = ("2 * 6371 * asin(sqrt(pow(sin(radians(lat - ?) / 2), 2) + cos(radians(?)) * cos(radians(lat)) "
              "* pow(sin(radians(lon - ?) / 2), 2)))")
        rows = (f"SELECT callsign, ts, lat, lon, direct, {km} AS km FROM aprs_igate "
                "WHERE igate=? AND ts BETWEEN ? AND ? AND lat IS NOT NULL AND (direct OR NOT ?)")
        return f"SELECT max(km) AS km FROM ({rows}) GROUP BY callsign" if per_station else rows

    def igate_quantiles(self, at: tuple[float, float], igate: str, start: int, end: int, direct: bool,
                        per_station: bool) -> dict:
        lat, lon = at
        row = self.db.execute(f"SELECT COUNT(*) AS n, quantile_cont(km, 0.5) AS p50, quantile_cont(km, 0.9) AS p90, "
                              f"quantile_cont(km, 0.99) AS p99, max(km) AS max FROM ({self._igate_distances(per_station)})",
                              (lat, lat, lon, igate, start, end, direct)).fetchone()
        return dict(row)

    def igate_histogram(self, at: tuple[float, float], igate: str, start: int, end: int, direct: bool,
                        per_station: bool, width: float) -> dict[int, int]:
        """{bin: count}, bin i holding [i·width, (i+1)·width) km."""
        lat, lon = at
        return {int(r["b"]): r["n"] for r in self.db.execute(
            f"SELECT floor(km / ?) AS b, COUNT(*) AS n FROM ({self._igate_distances(per_station)}) GROUP BY 1",
            (width, lat, lat, lon, igate, start, end, direct))}

    def igate_farthest(self, at: tuple[float, float], igate: str, start: int, end: int, direct: bool,
                       limit: int = 10) -> list[dict]:
        """The stations heard farthest away, each at its farthest packet."""
        lat, lon = at
        return [dict(r) for r in self.db.execute(
            f"SELECT callsign, arg_max(ts, km) AS ts, arg_max(lat, km) AS lat, arg_max(lon, km) AS lon, "
            f"arg_max(direct, km) AS direct, max(km) AS km, COUNT(*) AS packets FROM ({self._igate_distances(False)}) "
            f"GROUP BY callsign ORDER BY km DESC LIMIT ?", (lat, lat, lon, igate, start, end, direct, limit))]
