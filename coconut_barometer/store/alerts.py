"""Persisted Web Push alert attempts and their source evidence."""
from __future__ import annotations

import json

from .db import Database


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

    def alert_log_recent(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute(
            "SELECT \"at\", alert_key, title, body, url, evidence, device_id, device_label, username, status, detail "
            "FROM alerts_log ORDER BY id DESC LIMIT ?", (min(1000, max(1, limit)),)).fetchall()
        return [{**dict(row), "evidence": json.loads(row["evidence"] or "{}")} for row in rows]
