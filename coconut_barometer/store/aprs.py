"""APRS positions heard from APRS-IS for the callsigns in Settings → APRS, metric like the weather.
  aprs_positions   one row per position packet, keyed by (callsign, ts): where, altitude (m), speed (m/s),
                   course, the symbol (table + code), the comment, the path and the raw packet
  aprs_igate       one row per packet the igate gated: the sender, where it said it was (objects and items,
                   which place something else, and packets with no position have none), whether it was heard
                   direct, the path and the raw packet; and its altitude (m), symbol, comment and tocall (#118).
                   The igate's own last position is meta aprs_igate_at."""
from __future__ import annotations

import json
import math
import time

from ..aprs.igate import TYPES, UNKNOWN, bin_width, details, digi_hops, station_type
from .db import Database

TRACK_COLS = "ts, lat, lon, altitude, speed, course"
KM = ("2 * 6371 * asin(sqrt(pow(sin(radians(lat - ?) / 2), 2) + cos(radians(?)) * cos(radians(lat)) "
      "* pow(sin(radians(lon - ?) / 2), 2)))")     # from (?, ?, ? = lat, lat, lon)
BEARING = ("(degrees(atan2(sin(radians(lon - ?)) * cos(radians(lat)), cos(radians(?)) * sin(radians(lat)) "
           "- sin(radians(?)) * cos(radians(lat)) * cos(radians(lon - ?)))) + 360) % 360")   # (lon, lat, lat, lon)
REGULAR_DAYS, REGULAR_OF = 7, 14                    # a regular: heard on at least 7 of the last 14 days
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
        d = details(packet)
        self.db.execute("INSERT INTO aprs_igate (igate, callsign, ts, lat, lon, direct, format, path, raw, altitude, "
                        "symbol, comment, tocall) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (igate, packet.get("from"), int(ts or time.time()), packet["latitude"] if placed else None,
                         packet["longitude"] if placed else None, direct, packet.get("format"),
                         ",".join(packet.get("path") or []), packet.get("raw"),
                         d["altitude"], d["symbol"], d["comment"], d["tocall"]))

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
        rows = (f"SELECT callsign, ts, lat, lon, direct, {KM} AS km FROM aprs_igate "
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

    def igate_stations(self, at: tuple[float, float] | None, igate: str, start: int, end: int, offset: int = 0,
                       limit: int = 1000) -> dict:
        """Who the igate heard in the window (#118), busiest first: first and last heard in it, packets, the median
        distance of its positions (with `at`), its type (igate.station_type), whether it's new (never heard before
        the window) and a regular (heard on REGULAR_DAYS of the REGULAR_OF days to `end`). Also `days`: per local
        day (UTC + `offset` seconds), ["YYYY-MM-DD", stations, new stations]; per week (from Monday) past 90 days."""
        km, args = (KM, (at[0], at[0], at[1])) if at else ("NULL", ())
        rows = self.db.execute(
            f"WITH w AS (SELECT callsign, ts, format, symbol, {km} AS km FROM aprs_igate WHERE igate=? AND ts BETWEEN ? AND ?), "
            "ever AS (SELECT callsign, MIN(ts) AS first_ever FROM aprs_igate WHERE igate=? GROUP BY 1), "
            "recent AS (SELECT callsign, COUNT(DISTINCT (ts + ?) // 86400) AS days FROM aprs_igate "
            "WHERE igate=? AND ts > ? AND ts <= ? GROUP BY 1) "
            "SELECT callsign, MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS packets, median(km) AS km, "
            "arg_max(symbol, ts) FILTER (WHERE symbol IS NOT NULL AND coalesce(format, '') NOT IN ('object', 'item')) AS symbol, "
            "bool_or(format = 'wx') AS wx, bool_and(coalesce(format, '') IN ('object', 'item')) AS objects_only, "
            "any_value(first_ever) AS first_ever, coalesce(any_value(recent.days), 0) AS recent_days "
            "FROM w JOIN ever USING (callsign) LEFT JOIN recent USING (callsign) GROUP BY callsign ORDER BY packets DESC, callsign",
            (*args, igate, start, end, igate, offset, igate, end - REGULAR_OF * 86400, end)).fetchall()
        stations = [{"callsign": r["callsign"], "first_ts": r["first_ts"], "last_ts": r["last_ts"], "packets": r["packets"],
                     "km": r["km"], "symbol": r["symbol"], "type": station_type(r["symbol"], r["wx"], r["objects_only"]),
                     "first_ever": r["first_ever"], "new": r["first_ever"] >= start, "regular": r["recent_days"] >= REGULAR_DAYS}
                    for r in rows]
        step, shift = (86400, 0) if end - start <= 90 * 86400 else (7 * 86400, 4 * 86400)   # day 4 of 1970: a Monday
        bucket = lambda col: f"(({col} + {int(offset) - shift}) // {step}) * {step} - {int(offset) - shift}"
        heard = dict(self.db.execute(f"SELECT {bucket('ts')} AS b, COUNT(DISTINCT callsign) FROM aprs_igate "
                                     "WHERE igate=? AND ts BETWEEN ? AND ? GROUP BY 1", (igate, start, end)).fetchall())
        new = dict(self.db.execute(f"SELECT {bucket('first_ts')} AS b, COUNT(*) FROM (SELECT MIN(ts) AS first_ts "
                                   "FROM aprs_igate WHERE igate=? GROUP BY callsign) WHERE first_ts BETWEEN ? AND ? GROUP BY 1",
                                   (igate, start, end)).fetchall())
        types = {t: 0 for t in TYPES}
        for s in stations:
            types[s["type"]] += 1
        return {"stations": stations[:limit], "count": len(stations), "new": sum(s["new"] for s in stations),
                "regular": sum(s["regular"] for s in stations), "types": types, "bucket": step,
                "days": [[time.strftime("%Y-%m-%d", time.gmtime(b + offset)), heard[b], new.get(b, 0)] for b in sorted(heard)]}

    def igate_high(self, igate: str, since: int, until: int, metres: float) -> list[dict]:
        """Senders placed above `metres` between since (exclusive) and until, each at its highest (#118)."""
        return [dict(r) for r in self.db.execute(
            "SELECT callsign, max(altitude) AS altitude, arg_max(ts, altitude) AS ts, arg_max(lat, altitude) AS lat, "
            "arg_max(lon, altitude) AS lon, arg_max(direct, altitude) AS direct FROM aprs_igate "
            "WHERE igate=? AND ts > ? AND ts <= ? AND lat IS NOT NULL AND altitude >= ? GROUP BY callsign ORDER BY callsign",
            (igate, since, until, metres))]

    def _igate_sectored(self, sectors: int) -> str:
        """Each packet's distance (km) and sector of bearing from the igate (sector 0 centred on north), for
        igate_sectors. Takes lat, lat, lon, lon, lat, lat, lon, then igate, start, end, direct."""
        half = 180 / sectors
        return (f"SELECT {KM} AS km, CAST(floor((({BEARING}) + {half}) % 360 / {2 * half}) AS INTEGER) AS sector "
                "FROM aprs_igate WHERE igate=? AND ts BETWEEN ? AND ? AND lat IS NOT NULL AND (direct OR NOT ?)")

    def igate_sectors(self, at: tuple[float, float], igate: str, start: int, end: int, direct: bool,
                      sectors: int) -> list[dict]:
        """Distance by direction (#115): per sector, its packets, median and 99th-percentile distance, and a histogram
        of its distances ({bin_km, counts, beyond}) binned to its own 99th percentile, as for the whole."""
        lat, lon = at
        args = (lat, lat, lon, lon, lat, lat, lon, igate, start, end, direct)
        sql = self._igate_sectored(sectors)
        q = {r["sector"]: dict(r) for r in self.db.execute(
            f"SELECT sector, COUNT(*) AS n, quantile_cont(km, 0.5) AS p50, quantile_cont(km, 0.99) AS p99 "
            f"FROM ({sql}) GROUP BY 1", args)}
        widths = [bin_width(q[s]["p99"]) if s in q else 1.0 for s in range(sectors)]
        hist: dict[int, dict[int, int]] = {}
        for r in self.db.execute(f"SELECT sector, floor(km / list_extract(?::DOUBLE[], sector + 1)) AS b, COUNT(*) AS n "
                                 f"FROM ({sql}) GROUP BY 1, 2", (widths, *args)):
            hist.setdefault(r["sector"], {})[int(r["b"])] = r["n"]
        out = []
        for s in range(sectors):
            row = q.get(s) or {"n": 0, "p50": None, "p99": None}
            nbins = max(1, math.ceil((row["p99"] or 0) / widths[s] + 1e-9))
            counts = [hist.get(s, {}).get(i, 0) for i in range(nbins)]
            out.append({"sector": s, "n": row["n"], "p50_km": row["p50"], "bin_km": widths[s], "counts": counts,
                        "beyond": row["n"] - sum(counts)})
        return out

    def igate_digis(self, igate: str, start: int, end: int, edges: int = 300) -> dict:
        """The RF network feeding the igate over the window (#117), from its packets' paths: each digi with the
        packets it relayed, its senders (busiest first), when last heard and its share of all the igate gated; how
        many came direct, through one digi, or more; the links sender → digi → … → igate by packets (the busiest
        `edges`); and where each station on them last placed itself, when it did."""
        total = 0
        hops = {"direct": 0, "one": 0, "more": 0}
        digis: dict[str, dict] = {}
        links: dict[tuple[str, str], int] = {}
        for call, path, n, last in self.db.execute(
                "SELECT callsign, path, COUNT(*), MAX(ts) FROM aprs_igate WHERE igate=? AND ts BETWEEN ? AND ? GROUP BY 1, 2",
                (igate, start, end)).fetchall():
            relay = digi_hops((path or "").split(","))
            total += n
            hops["direct" if not relay else "one" if len(relay) == 1 else "more"] += n
            for d in dict.fromkeys(relay):
                x = digis.setdefault(d, {"callsign": d, "packets": 0, "senders": {}, "last_ts": 0})
                x["packets"] += n
                x["senders"][call] = x["senders"].get(call, 0) + n
                x["last_ts"] = max(x["last_ts"], last)
            chain = [call, *relay, igate]
            for a, b in zip(chain, chain[1:]):
                if a != b and UNKNOWN not in (a, b):
                    links[(a, b)] = links.get((a, b), 0) + n
        busiest = sorted(links.items(), key=lambda kv: -kv[1])[:edges]
        named = {c for (a, b), _ in busiest for c in (a, b)} | set(digis) - {UNKNOWN}
        places = {}
        if named:
            for call, lat, lon in self.db.execute(
                    "SELECT callsign, arg_max(lat, ts), arg_max(lon, ts) FROM aprs_igate WHERE igate=? AND lat IS NOT NULL "
                    "AND callsign IN (SELECT unnest(?::VARCHAR[])) GROUP BY 1", (igate, sorted(named))).fetchall():
                places[call] = [lat, lon]
        rows = sorted(digis.values(), key=lambda x: (-x["packets"], x["callsign"]))
        for x in rows:
            x["share"] = x["packets"] / total if total else 0
            x["sender_count"] = len(x["senders"])
            x["senders"] = [c for c, _ in sorted(x["senders"].items(), key=lambda kv: -kv[1])[:20]]
            x["at"] = places.get(x["callsign"])
        return {"total": total, "hops": hops, "digis": rows, "edges": [[a, b, n] for (a, b), n in busiest], "places": places}
