"""Station QC's samples and results (#105): what each reference said at each station, and each check's runs.

Keyed by the Tempest device (wx_obs's key), each at its station's place. Times are epoch seconds: an RTMA sample at
its analysis's valid time, a radar sample at its frame's, a GLM flash at its file's start."""
from __future__ import annotations

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
            self.db.execute("DELETE FROM qc_runs WHERE run_at < ?", (before,))

    # ---- what the checks read ---------------------------------------------------------------------------------
    def qc_temperature_pairs(self, device_id: int, start: int, end: int, within: int = 150) -> list[tuple]:
        """Each RTMA sample in [start, end] with the station's mean temperature within `within` seconds of it:
        (ts, rtma °C, rtma cell height m, station °C)."""
        return [tuple(r) for r in self.db.execute(
            "SELECT r.ts, r.t, r.hgt, avg(o.temp) FROM qc_rtma r JOIN wx_obs o ON o.device_id = r.device_id "
            "AND o.ts BETWEEN r.ts - ? AND r.ts + ? WHERE r.device_id = ? AND r.ts BETWEEN ? AND ? "
            "AND r.t IS NOT NULL AND o.temp IS NOT NULL GROUP BY r.ts, r.t, r.hgt ORDER BY r.ts",
            (within, within, device_id, start, end)).fetchall()]

    # ---- results ----------------------------------------------------------------------------------------------
    def qc_save_run(self, device_id: int, suite: str, run_at: int, start: int, end: int, result: dict) -> None:
        with self.db:
            self.db.execute("INSERT INTO qc_runs VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                            (device_id, suite, run_at, start, end, result["status"],
                             json.dumps(result.get("metrics") or {}, separators=(",", ":"))))

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
