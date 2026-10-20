"""The review queue (per-photo proposed changes) and decisions (what was applied,
rejected or failed, so nothing settled is proposed again)."""
from __future__ import annotations

import json
import time
from typing import Iterable

from .db import Database


class QueueStore(Database):
    SORTS = {"newest": "p.date_upload DESC", "oldest": "p.date_upload ASC", "views": "p.views DESC",
             "faves": "p.faves DESC", "comments": "p.comments DESC"}

    def queue_ids(self, statuses: Iterable[str] | None = None, sort: str = "newest") -> list[str]:
        if statuses is None:
            return [r[0] for r in self.db.execute("SELECT photo_id FROM queue")]
        statuses = list(statuses)
        marks = ",".join("?" * len(statuses))
        return [r[0] for r in self.db.execute(
            f"SELECT q.photo_id FROM queue q JOIN photos p ON p.id = q.photo_id "
            f"WHERE q.status IN ({marks}) ORDER BY {self.SORTS.get(sort, self.SORTS['newest'])}", statuses)]

    def unqueued_ids(self, sort: str = "newest") -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT p.id FROM photos p WHERE p.id NOT IN (SELECT photo_id FROM queue) "
            f"ORDER BY {self.SORTS.get(sort, self.SORTS['newest'])}")]

    def queue_item(self, photo_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM queue WHERE photo_id=?", (photo_id,)).fetchone()
        if not r:
            return None
        return {**dict(r), "changes": json.loads(r["changes"] or "[]"), "result": json.loads(r["result"] or "null")}

    def queue_put(self, photo_id: str, status: str, changes: list[dict]) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO queue (photo_id, status, changes, built_at) VALUES (?, ?, ?, ?)",
                            (photo_id, status, json.dumps(changes), int(time.time())))

    def queue_finish(self, photo_id: str, status: str, result) -> None:
        with self.db:
            self.db.execute("UPDATE queue SET status=?, reviewed_at=?, result=? WHERE photo_id=?",
                            (status, int(time.time()), json.dumps(result), photo_id))

    def queue_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.execute("SELECT status, COUNT(*) FROM queue GROUP BY status")}

    def decisions(self) -> dict[tuple[str, str, str], str]:
        return {
            (r["photo_id"], r["action"], r["target"]): r["status"]
            for r in self.db.execute("SELECT photo_id, action, target, status FROM decisions")
        }

    def record(self, photo_id: str, action: str, target: str, status: str, detail: str = "") -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
                (photo_id, action, target, status, detail, int(time.time())),
            )

    def applied(self, action: str) -> dict[str, tuple[str, int]]:
        """photo_id -> (target, applied_at) of the latest applied change of this kind."""
        out: dict[str, tuple[str, int]] = {}
        for r in self.db.execute("SELECT photo_id, target, at FROM decisions WHERE action=? AND status='applied' "
                                 "ORDER BY at", (action,)):
            out[r[0]] = (r[1], r[2])
        return out

    def applied_since(self, since: int) -> list[tuple[str, str, str]]:
        """(photo_id, action, target) applied at or after `since`, oldest first."""
        return [tuple(r) for r in self.db.execute(
            "SELECT photo_id, action, target FROM decisions WHERE status='applied' AND at >= ? ORDER BY at", (since,))]
