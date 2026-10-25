"""The review queue (per-photo proposed changes) and decisions (a log of what was applied or
failed, for Analytics; it doesn't stop anything being proposed again).

A photo is in the queue from being sent to review until it's applied or ignored:
  new       waiting to be processed (review.when_queued: queue, or processing failed)
  pending   ready: its proposed changes (maybe none), or the ones whose apply failed twice
  applying  your choices saved, the worker applying them (resumed after a restart)"""
from __future__ import annotations

import json
import time
from typing import Iterable

from .db import Database


class QueueStore(Database):
    SORTS = {"newest": "p.date_upload DESC", "oldest": "p.date_upload ASC", "views": "p.views DESC",
             "faves": "p.faves DESC", "comments": "p.comments DESC"}
    SORTS = {k: v + ", p.id" for k, v in SORTS.items()}

    def queue_ids(self, statuses: Iterable[str] | None = None, sort: str = "newest") -> list[str]:
        if statuses is None:
            return [r[0] for r in self.db.execute("SELECT photo_id FROM queue")]
        statuses = list(statuses)
        marks = ",".join("?" * len(statuses))
        return [r[0] for r in self.db.execute(
            f"SELECT q.photo_id FROM queue q JOIN photos p ON p.id = q.photo_id "
            f"WHERE q.status IN ({marks}) ORDER BY {self.SORTS.get(sort, self.SORTS['newest'])}", statuses)]

    def processed_ids(self) -> set[str]:
        """Photos the vision model has looked at, with any settings: processed at least once."""
        return {r[0] for r in self.db.execute("SELECT DISTINCT photo_id FROM classifications")}

    def latest_upload(self) -> int | None:
        return self.db.execute("SELECT MAX(date_upload) FROM photos").fetchone()[0]

    def queue_new_photos(self, uploaded_after: int) -> list[str]:
        """Photos uploaded after that time and not in the queue yet go in as "new" (to be processed:
        now or when opened, by review.when_queued). Returns their ids, newest first."""
        ids = [r[0] for r in self.db.execute(
            "SELECT id FROM photos WHERE date_upload > ? AND id NOT IN (SELECT photo_id FROM queue) "
            "ORDER BY date_upload DESC, id", (uploaded_after,))]
        for pid in ids:
            self.queue_put(pid, "new", [])
            self.queue_finish(pid, "new", {"new_upload": True})
        return ids

    def missing_location_ids(self) -> list[str]:
        """Photos with no geotag that aren't in review, newest first. Not ones whose location you
        passed on (unticked, or ignored the photo) since: those come back only if you send them."""
        return [r[0] for r in self.db.execute(
            "SELECT id FROM photos WHERE lat IS NULL AND id NOT IN (SELECT photo_id FROM queue) "
            "AND id NOT IN (SELECT photo_id FROM decisions WHERE action='set_location' AND status='rejected') "
            "ORDER BY date_upload DESC, id")]

    def queue_item(self, photo_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM queue WHERE photo_id=?", (photo_id,)).fetchone()
        if not r:
            return None
        return {**dict(r), "changes": json.loads(r["changes"] or "[]"), "result": json.loads(r["result"] or "null")}

    def queue_put(self, photo_id: str, status: str, changes: list[dict]) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO queue (photo_id, status, changes, built_at) VALUES (?, ?, ?, ?)",
                            (photo_id, status, json.dumps(changes), int(time.time())))

    def queue_add(self, photo_id: str) -> bool:
        """Into the queue as "new", unless it's there already. True if it was added."""
        if self.db.execute("SELECT 1 FROM queue WHERE photo_id=?", (photo_id,)).fetchone():
            return False
        self.queue_put(photo_id, "new", [])
        return True

    def queue_remove(self, photo_id: str) -> None:
        """Out of the queue: applied, or ignored."""
        with self.db:
            self.db.execute("DELETE FROM queue WHERE photo_id=?", (photo_id,))

    # ---- jobs you asked for, kept until done so a restart doesn't lose them ------------------------------
    def job_requested(self, kind: str, photo_id: str = "") -> None:
        """Asked again while waiting: keeps its first time, so it keeps its place."""
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO requested_jobs (kind, photo_id, "at") VALUES (?, ?, ?)', (kind, photo_id, int(time.time())))

    def job_done(self, kind: str, photo_id: str = "") -> None:
        with self.db:
            self.db.execute("DELETE FROM requested_jobs WHERE kind=? AND photo_id=?", (kind, photo_id))

    def jobs_requested(self) -> list[tuple[str, str]]:
        """In the order asked."""
        return [(r["kind"], r["photo_id"]) for r in self.db.execute("SELECT kind, photo_id FROM requested_jobs ORDER BY seq")]

    def failures_since(self, photo_id: str, since: int) -> list[str]:
        """Why this photo's changes didn't go through (failed, or Flickr said no), since that time."""
        return [r[0] for r in self.db.execute(
            'SELECT DISTINCT detail FROM decisions WHERE photo_id=? AND status IN (\'failed\', \'deferred\', \'refused\') '
            'AND "at" >= ? AND detail <> \'\'', (photo_id, since))]

    def turned_down_since(self, photo_id: str, since: int) -> set[tuple[str, str]]:
        """(action, target) of this photo's changes Flickr turned down since then for a reason that trying
        again straight away won't fix (a group's limit or rules): not retried."""
        return {(r[0], r[1]) for r in self.db.execute(
            'SELECT action, target FROM decisions WHERE photo_id=? AND status IN (\'deferred\', \'refused\') AND "at" >= ?',
            (photo_id, since))}

    def refused_groups(self) -> dict[str, set[str]]:
        """photo_id -> the groups that refused it for good ("*": every group, the photo is in as many as it may be)."""
        from ..apply import POOL_REFUSED
        out: dict[str, set[str]] = {}
        for pid, target, detail in self.db.execute(
                "SELECT photo_id, target, detail FROM decisions WHERE action='add_to_group' AND status='refused'"):
            out.setdefault(pid, set()).add("*" if detail.endswith(POOL_REFUSED[4]) else target)
        return out

    def queue_finish(self, photo_id: str, status: str, result) -> None:
        with self.db:
            self.db.execute("UPDATE queue SET status=?, reviewed_at=?, result=? WHERE photo_id=?",
                            (status, int(time.time()), json.dumps(result), photo_id))

    def queue_counts(self) -> dict[str, int]:
        """Photos in review by status: those in the library only, as queue_ids lists them."""
        return {r[0]: r[1] for r in self.db.execute(
            "SELECT q.status, COUNT(*) FROM queue q JOIN photos p ON p.id = q.photo_id GROUP BY q.status")}

    def queue_prune(self) -> int:
        """Review rows, and jobs asked for, whose photo is no longer in the library (deleted on Flickr before
        delete_photos took them with it). Returns how many review rows went."""
        gone = "NOT EXISTS (SELECT 1 FROM photos p WHERE p.id = {}.photo_id)"
        with self.db:
            n = self.db.execute(f"SELECT COUNT(*) FROM queue q WHERE {gone.format('q')}").fetchone()[0]
            self.db.execute(f"DELETE FROM queue q WHERE {gone.format('q')}")
            self.db.execute(f"DELETE FROM requested_jobs j WHERE j.photo_id <> '' AND {gone.format('j')}")
        return n

    def record_applied(self, photo_id: str, changes: list[dict]) -> None:
        """An apply, for undoing it: per change written, what it replaced (see review.app._undo_entry)."""
        with self.db:
            self.db.execute('INSERT INTO applied (photo_id, "at", changes) VALUES (?, ?, ?)',
                            (photo_id, int(time.time()), json.dumps(changes)))

    def last_applied(self, photo_id: str) -> dict | None:
        """This photo's latest apply not undone yet."""
        r = self.db.execute('SELECT * FROM applied WHERE photo_id=? AND undone_at IS NULL ORDER BY id DESC LIMIT 1',
                            (photo_id,)).fetchone()
        return {**dict(r), "changes": json.loads(r["changes"])} if r else None

    def recent_applied(self, limit: int = 50) -> list[dict]:
        """The latest applies, newest first, undone or not."""
        return [{**dict(r), "changes": json.loads(r["changes"]), "result": json.loads(r["result"] or "null")}
                for r in self.db.execute('SELECT * FROM applied ORDER BY id DESC LIMIT ?', (limit,))]

    def mark_undone(self, applied_id: int, result) -> None:
        with self.db:
            self.db.execute("UPDATE applied SET undone_at=?, result=? WHERE id=?",
                            (int(time.time()), json.dumps(result), applied_id))

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
        for r in self.db.execute('SELECT photo_id, target, "at" FROM decisions WHERE action=? AND status=\'applied\' '
                                 'ORDER BY "at"', (action,)):
            out[r[0]] = (r[1], r[2])
        return out

    def applied_since(self, since: int) -> list[tuple[str, str, str]]:
        """(photo_id, action, target) applied at or after `since`, oldest first."""
        return [tuple(r) for r in self.db.execute(
            'SELECT photo_id, action, target FROM decisions WHERE status=\'applied\' AND "at" >= ? ORDER BY "at"', (since,))]
