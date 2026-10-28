"""Station QC's samples and results (#105): what each reference said at each station, and each check's runs.

Keyed by the Tempest device (wx_obs's key), each at its station's place. Times are epoch seconds: an RTMA sample at
its analysis's valid time, a radar sample at its frame's, a GLM flash at its file's start."""
from __future__ import annotations

import bisect
import json

from .db import Database

RTMA = ("hgt", "pres", "t", "td", "u", "v", "gust", "tcdc")
TABLES = {"rtma": "qc_rtma", "radar": "qc_radar", "glm": "qc_glm", "alerts": "qc_alerts"}


class QcStore(Database):
    def qc_stations(self) -> list[dict]:
        """The Tempest (ST) devices whose station has a place: {device_id, name, lat, lon, elevation, timezone}."""
        rows = self.db.execute(
            "SELECT d.device_id, s.name, s.lat, s.lon, s.elevation, s.timezone FROM wx_devices d "
            "JOIN wx_stations s ON s.station_id = d.station_id "
            "WHERE d.device_type = 'ST' AND s.lat IS NOT NULL AND s.lon IS NOT NULL ORDER BY s.name").fetchall()
        return [dict(r) for r in rows]

    # ---- samples ----------------------------------------------------------------------------------------------
    def qc_save_rtma(self, device_id: int, ts: int, p: dict) -> None:
        with self.db:
            self.db.execute(f"INSERT INTO qc_rtma (device_id, ts, {', '.join(RTMA)}) VALUES (?, ?, {', '.join('?' * len(RTMA))}) "
                            "ON CONFLICT DO NOTHING", (device_id, ts, *(p.get(k) for k in RTMA)))

    def qc_rtma_times(self, since: int) -> set[int]:
        """The analysis times sampled (at any station) since `since`."""
        return {r[0] for r in self.db.execute("SELECT DISTINCT ts FROM qc_rtma WHERE ts >= ?", (since,)).fetchall()}

    def qc_save_radar(self, rows: list[tuple]) -> None:
        """(device_id, ts, dbz at the station, the most within reach)."""
        with self.db:
            for r in rows:
                self.db.execute("INSERT INTO qc_radar VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING", r)

    def qc_save_glm(self, rows: list[tuple]) -> None:
        """(device_id, ts, lat, lon, km, bearing) of flashes within reach."""
        with self.db:
            for r in rows:
                self.db.execute("INSERT INTO qc_glm VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING", r)

    def qc_save_glm_cover(self, start: int, end: int) -> None:
        """[start, end] was watched for GLM flashes (#159): the newest span stretched if it reaches start, else a new
        one. The first span takes in the flashes kept before coverage was tracked (from the oldest of them)."""
        last = self.db.execute('SELECT start, "end" FROM qc_glm_cover ORDER BY start DESC LIMIT 1').fetchone()
        with self.db:
            if last is None:
                first = self.db.execute("SELECT min(ts) FROM qc_glm").fetchone()[0]
                self.db.execute("INSERT INTO qc_glm_cover VALUES (?, ?)", (min(start, first if first is not None else start), end))
            elif last[1] >= start:
                self.db.execute('UPDATE qc_glm_cover SET "end" = ? WHERE start = ?', (max(last[1], end), last[0]))
            else:
                self.db.execute("INSERT INTO qc_glm_cover VALUES (?, ?)", (start, end))

    def qc_glm_cover(self, start: int, end: int) -> list[tuple[int, int]]:
        """The spans GLM was watched that overlap [start, end]: (start, end), oldest first."""
        return [tuple(r) for r in self.db.execute('SELECT start, "end" FROM qc_glm_cover WHERE "end" >= ? AND start <= ? '
                                                  "ORDER BY start", (start, end)).fetchall()]

    def qc_save_alerts(self, device_id: int, alerts: list[dict], now: int) -> None:
        """The NWS alerts covering the station now: new ones added, ones still in effect seen again."""
        with self.db:
            for a in alerts:
                self.db.execute(
                    "INSERT INTO qc_alerts VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (device_id, id) DO UPDATE SET "
                    "last_seen = excluded.last_seen, ends = excluded.ends",
                    (device_id, a["id"], a["event"], a.get("onset"), a.get("ends"), now, now))

    def qc_save_front(self, valid: int, analysis: dict) -> bool:
        """A WPC analysis (its fronts near the stations); False if it was kept already."""
        if self.db.execute("SELECT 1 FROM qc_fronts WHERE valid = ?", (valid,)).fetchone():
            return False
        with self.db:
            self.db.execute("INSERT INTO qc_fronts VALUES (?, ?)", (valid, json.dumps(analysis, separators=(",", ":"))))
        return True

    def qc_sampled(self) -> dict:
        """Per source: how many samples are kept, the oldest and the newest."""
        out = {}
        for name, table in TABLES.items():
            col = "last_seen" if name == "alerts" else "ts"
            n, lo, hi = self.db.execute(f"SELECT count(*), min({col}), max({col}) FROM {table}").fetchone()
            out[name] = {"count": n, "oldest": lo, "newest": hi}
        n, lo, hi = self.db.execute("SELECT count(*), min(valid), max(valid) FROM qc_fronts").fetchone()
        out["fronts"] = {"count": n, "oldest": lo, "newest": hi}
        return out

    def qc_prune(self, before: int) -> None:
        with self.db:
            for name, table in TABLES.items():
                self.db.execute(f"DELETE FROM {table} WHERE {'last_seen' if name == 'alerts' else 'ts'} < ?", (before,))
            self.db.execute("DELETE FROM qc_fronts WHERE valid < ?", (before,))
            self.db.execute('DELETE FROM qc_glm_cover WHERE "end" < ?', (before,))
            self.db.execute("DELETE FROM qc_runs WHERE run_at < ?", (before,))
            self.db.execute('DELETE FROM qc_flags WHERE "end" < ?', (before,))

    # ---- what the checks read ---------------------------------------------------------------------------------
    def qc_rtma_pairs(self, device_id: int, start: int, end: int, within: int = 150) -> list[dict]:
        """Each RTMA sample in [start, end] with the station's means within `within` seconds of it: {ts, the sample's
        fields (hgt, pres, t, td, u, v, gust, tcdc), and the station's temp, dew (from its humidity), humidity,
        pressure, wind, gust, solar, and wind_e, wind_n: its wind averaged as a vector (the direction it's from, east
        and north, m/s), since an average of directions goes wrong across north}. Only samples the station has
        readings around."""
        from .weather import DEW_SQL

        rows = self.db.execute(
            f"SELECT r.ts, {', '.join('r.' + k for k in RTMA)}, avg(o.temp), avg({DEW_SQL}), avg(o.humidity), avg(o.pressure), "
            "avg(o.wind_avg), max(o.wind_gust), avg(o.solar), avg(o.wind_avg * sin(radians(o.wind_dir))), "
            "avg(o.wind_avg * cos(radians(o.wind_dir))) FROM qc_rtma r JOIN wx_obs o ON o.device_id = r.device_id "
            "AND o.ts BETWEEN r.ts - ? AND r.ts + ? WHERE r.device_id = ? AND r.ts BETWEEN ? AND ? "
            f"GROUP BY r.ts, {', '.join('r.' + k for k in RTMA)} ORDER BY r.ts", (within, within, device_id, start, end)).fetchall()
        names = ("ts", *RTMA, "s_temp", "s_dew", "s_humidity", "s_pressure", "s_wind", "s_gust", "s_solar", "s_wind_e", "s_wind_n")
        return [dict(zip(names, r)) for r in rows]

    def qc_glm_flashes(self, device_id: int, start: int, end: int) -> list[tuple]:
        """The GLM flashes kept for the station in [start, end]: (ts, km, bearing), oldest first."""
        return [tuple(r) for r in self.db.execute("SELECT ts, km, bearing FROM qc_glm WHERE device_id = ? AND ts BETWEEN ? AND ? "
                                                  "ORDER BY ts", (device_id, start, end)).fetchall()]

    def qc_strikes(self, device_id: int, start: int, end: int, within: int = 60) -> list[tuple]:
        """The station's lightning strikes in [start, end]: (ts, km), oldest first. Each strike the live stream sent,
        and the minutes with strikes from the station's history where the stream has none (not connected then):
        a minute's count of them at its average distance."""
        events = [(r[0], r[1]) for r in self.db.execute(
            "SELECT ts, value FROM wx_events WHERE device_id = ? AND kind = 'strike' AND ts BETWEEN ? AND ? AND value > 0 "
            "ORDER BY ts", (device_id, start, end)).fetchall()]
        times = [t for t, _ in events]
        out = list(events)
        for ts, km, n in self.db.execute(
                "SELECT ts, strike_dist, strike_count FROM wx_obs WHERE device_id = ? AND ts BETWEEN ? AND ? "
                "AND strike_count > 0 AND strike_dist > 0", (device_id, start, end)).fetchall():
            k = bisect.bisect_left(times, ts - within)
            if k < len(times) and times[k] <= ts + within:
                continue
            out += [(ts, km)] * int(n)
        return sorted(out)

    def qc_front_analyses(self, start: int, end: int) -> list[tuple[int, dict]]:
        """The WPC analyses kept, valid in [start, end]: (valid, {fronts, centers}), oldest first."""
        return [(r[0], json.loads(r[1])) for r in self.db.execute(
            "SELECT valid, analysis FROM qc_fronts WHERE valid BETWEEN ? AND ? ORDER BY valid", (start, end)).fetchall()]

    # ---- results ----------------------------------------------------------------------------------------------
    def qc_save_run(self, device_id: int, suite: str, run_at: int, start: int, end: int, result: dict) -> None:
        with self.db:
            self.db.execute('INSERT INTO qc_runs VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET start = excluded.start, '
                            '"end" = excluded."end", status = excluded.status, metrics = excluded.metrics',   # a rerun: what it says now
                            (device_id, suite, run_at, start, end, result["status"],
                             json.dumps(result.get("metrics") or {}, separators=(",", ":"))))

    def qc_save_flags(self, device_id: int, suite: str, start: int, end: int, flags: list[tuple]) -> None:
        """A check's flagged periods in [start, end], in place of what it flagged there before."""
        with self.db:
            self.db.execute('DELETE FROM qc_flags WHERE device_id = ? AND suite = ? AND "end" >= ? AND start <= ?',
                            (device_id, suite, start, end))
            for a, b, what in flags:
                self.db.execute("INSERT INTO qc_flags VALUES (?, ?, ?, ?, ?)", (device_id, suite, int(a), int(b), what))

    def qc_flags(self, device_id: int, since: int, per_suite: int = 3) -> dict[str, list[dict]]:
        """Each check's newest flags since `since` (its most recent per_suite), and how many there are in all."""
        out: dict[str, list[dict]] = {}
        for r in self.db.execute('SELECT suite, start, "end", what FROM qc_flags WHERE device_id = ? AND "end" >= ? '
                                 'ORDER BY suite, "end" DESC', (device_id, since)).fetchall():
            out.setdefault(r["suite"], []).append({"start": r["start"], "end": r["end"], "what": r["what"]})
        return {k: {"count": len(v), "newest": v[:per_suite]} for k, v in out.items()}

    def qc_latest(self, device_id: int) -> dict[str, dict]:
        """Each check's newest run for the device, with `since`: when the unbroken run of that status began."""
        rows = self.db.execute('SELECT suite, run_at, start, "end", status, metrics FROM qc_runs WHERE device_id = ? '
                               "ORDER BY suite, run_at DESC", (device_id,)).fetchall()
        out: dict[str, dict] = {}
        broken: set[str] = set()                   # suites whose streak has ended (an older run had another status)
        for r in rows:
            k = r["suite"]
            if k not in out:
                out[k] = {"suite": k, "run_at": r["run_at"], "start": r["start"], "end": r["end"], "status": r["status"],
                          "metrics": json.loads(r["metrics"] or "{}"), "since": r["run_at"]}
            elif k not in broken and r["status"] == out[k]["status"]:
                out[k]["since"] = r["run_at"]
            else:
                broken.add(k)
        return out
