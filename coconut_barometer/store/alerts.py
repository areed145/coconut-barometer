"""Persisted Web Push alert attempts and their source evidence."""
from __future__ import annotations

import json

from .db import Database

FAILED = ("failed", "removed")       # a delivery that didn't arrive (removed: the subscription had expired)


class AlertsStore(Database):
    def alert_log_add(self, at: int, alert: dict, device: dict, status: str, detail: str = "") -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO alerts_log (\"at\", alert_key, title, body, url, evidence, device_id, device_label, "
                "username, status, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (at, alert["key"], alert["title"], alert["body"], alert["url"],
                 json.dumps(alert.get("evidence") or {}), device["id"], device["label"], device["username"],
                 status, detail))
            self.db.execute("DELETE FROM alerts_log WHERE id NOT IN "
                            "(SELECT id FROM alerts_log ORDER BY id DESC LIMIT 1000)")

    def alert_log_alert(self, at: int, key: str) -> dict | None:
        """One alert (#73): what it said and its evidence, and how it went to each device (or why it was held back)."""
        rows = [{**dict(r), "evidence": json.loads(r["evidence"] or "{}")} for r in self.db.execute(
            "SELECT \"at\", alert_key, title, body, url, evidence, device_label, username, status, detail "
            "FROM alerts_log WHERE \"at\"=? AND alert_key=? ORDER BY id", (at, key)).fetchall()]
        if not rows:
            return None
        first = rows[0]
        return {"at": first["at"], "key": first["alert_key"], "title": first["title"], "body": first["body"],
                "url": first["url"], "evidence": first["evidence"],
                "deliveries": [{k: r[k] for k in ("device_label", "username", "status", "detail")} for r in rows]}

    def alert_log_alerts(self, limit: int = 50, before: str | None = None, kind: str | None = None,
                         failed: bool = False) -> dict:
        """The Alerts page (#168): alerts newest first, one each (by when and key, however many devices it went to),
        with how it went to each. kind: the key's first part (gust, storm, callsign, ...); failed: only those with a
        delivery that failed. Paged by alert: `next` ("<at>-<id>") is the `before` for the page after, or None."""
        limit, where, having, args = min(200, max(1, limit)), [], [], []
        if kind:
            where.append("split_part(alert_key, ':', 1) = ?")
            args.append(kind)
        if failed:
            having.append(f"max(CASE WHEN status IN {FAILED} THEN 1 ELSE 0 END) = 1")
        if before:
            at, last = (int(x) for x in before.split("-"))
            having.append("(\"at\" < ? OR (\"at\" = ? AND max(id) < ?))")
            args += [at, at, last]
        groups = self.db.execute(
            f"SELECT \"at\", alert_key, max(id) AS last FROM alerts_log {'WHERE ' + ' AND '.join(where) if where else ''} "
            f"GROUP BY \"at\", alert_key {'HAVING ' + ' AND '.join(having) if having else ''} "
            "ORDER BY \"at\" DESC, last DESC LIMIT ?", (*args, limit + 1)).fetchall()
        more, groups = len(groups) > limit, groups[:limit]
        if not groups:
            return {"alerts": [], "next": None}
        rows: dict[tuple, list] = {(g["at"], g["alert_key"]): [] for g in groups}
        for r in self.db.execute(
                "SELECT \"at\", alert_key, title, body, url, evidence, device_label, username, status, detail "
                "FROM alerts_log WHERE \"at\" BETWEEN ? AND ? ORDER BY id",
                (min(g["at"] for g in groups), max(g["at"] for g in groups))).fetchall():
            if (r["at"], r["alert_key"]) in rows:
                rows[(r["at"], r["alert_key"])].append(r)
        stations = {str(d): n for d, n in self.db.execute(
            "SELECT d.device_id, s.name FROM wx_devices d JOIN wx_stations s ON s.station_id = d.station_id").fetchall()}
        alerts = []
        for (at, key), rs in rows.items():
            first, evidence = rs[0], json.loads(rs[0]["evidence"] or "{}")
            alerts.append({"at": at, "key": key, "kind": key.split(":")[0], "title": first["title"], "body": first["body"],
                           "url": first["url"], "evidence": evidence,
                           "subject": evidence.get("callsign") or stations.get(str(evidence.get("device_id", ""))) or "",
                           "deliveries": [{k: r[k] for k in ("device_label", "username", "status", "detail")} for r in rs]})
        return {"alerts": alerts, "next": f"{groups[-1]['at']}-{groups[-1]['last']}" if more else None}

    def alert_log_recent(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute(
            "SELECT \"at\", alert_key, title, body, url, evidence, device_id, device_label, username, status, detail "
            "FROM alerts_log ORDER BY id DESC LIMIT ?", (min(1000, max(1, limit)),)).fetchall()
        return [{**dict(row), "evidence": json.loads(row["evidence"] or "{}")} for row in rows]
