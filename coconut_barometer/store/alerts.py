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

    def alert_log_recent(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute(
            "SELECT \"at\", alert_key, title, body, url, evidence, device_id, device_label, username, status, detail "
            "FROM alerts_log ORDER BY id DESC LIMIT ?", (min(1000, max(1, limit)),)).fetchall()
        return [{**dict(row), "evidence": json.loads(row["evidence"] or "{}")} for row in rows]
