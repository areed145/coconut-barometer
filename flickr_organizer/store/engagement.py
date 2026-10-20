"""Engagement: account totals, groups and pools, comments, faves, and the counts
they were fetched at."""
from __future__ import annotations

import json
import time

from .db import Database


class EngagementStore(Database):
    def save_account_stats(self, stats: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO account_stats VALUES (?, ?)",
                            (time.strftime("%Y-%m-%d"), json.dumps(stats)))

    def replace_groups(self, groups: list[dict], pool: list[tuple[str, str, int]]) -> None:
        with self.db:
            self.db.execute("DELETE FROM flickr_groups")
            self.db.execute("DELETE FROM group_photos")
            self.db.executemany("INSERT INTO flickr_groups VALUES (:id, :name, :path_alias, :members, :pool_count, "
                                ":privacy, :info, :fetched_at)", groups)
            self.db.executemany("INSERT OR IGNORE INTO group_photos VALUES (?, ?, ?)", pool)

    def social_counts(self, kind: str) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.execute("SELECT photo_id, count FROM social_fetched WHERE kind=?", (kind,))}

    def save_comments(self, photo_id: str, comments: list[dict], count: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM comments WHERE photo_id=?", (photo_id,))
            self.db.executemany("INSERT OR REPLACE INTO comments VALUES (:id, :photo_id, :author, :author_name, "
                                ":real_name, :date_created, :permalink, :text)", comments)
            self.db.execute("INSERT OR REPLACE INTO social_fetched VALUES (?, 'comments', ?, ?)",
                            (photo_id, count, int(time.time())))

    def save_faves(self, photo_id: str, faves: list[dict], count: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM faves WHERE photo_id=?", (photo_id,))
            self.db.executemany("INSERT OR REPLACE INTO faves VALUES (:photo_id, :user_id, :username, "
                                ":real_name, :date_faved)", faves)
            self.db.execute("INSERT OR REPLACE INTO social_fetched VALUES (?, 'faves', ?, ?)",
                            (photo_id, count, int(time.time())))

    def engagement(self, photo_id: str) -> dict:
        """Everything the review tool shows about how a photo is doing."""
        q = lambda sql: self.db.execute(sql, (photo_id,)).fetchall()
        first = self.db.execute("SELECT day, views FROM photo_stats WHERE photo_id=? AND views IS NOT NULL "
                                "ORDER BY day LIMIT 1", (photo_id,)).fetchone()
        fetched = {r[0]: r[1] for r in q("SELECT kind, fetched_at FROM social_fetched WHERE photo_id=?")}
        return {
            "first_snapshot": dict(first) if first else None,
            "groups": [dict(r) for r in q("SELECT g.id, g.name, g.path_alias, g.members FROM group_photos gp "
                                          "JOIN flickr_groups g ON g.id = gp.group_id WHERE gp.photo_id=? "
                                          "ORDER BY g.members DESC")],
            "recent_comments": [dict(r) for r in q("SELECT author_name, real_name, date_created, text, permalink "
                                            "FROM comments WHERE photo_id=? ORDER BY date_created DESC LIMIT 5")],
            "recent_faves": [dict(r) for r in q("SELECT username, real_name, date_faved FROM faves "
                                                "WHERE photo_id=? ORDER BY date_faved DESC LIMIT 8")],
            "comments_fetched": "comments" in fetched, "faves_fetched": "faves" in fetched,
        }

    def photo_groups(self, photo_id: str) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT g.name FROM group_photos gp JOIN flickr_groups g ON g.id = gp.group_id WHERE gp.photo_id=? "
            "ORDER BY g.name COLLATE NOCASE", (photo_id,))]

    def group_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM flickr_groups").fetchone()[0]
