"""Per-day stats from Flickr's stats API: views/comments/faves per photo per day
(photo_daily), the account's daily totals (account_daily), and which days are done."""
from __future__ import annotations

import time

from .db import Database


class StatsStore(Database):
    def save_stats_day(self, day: str, totals: dict, photos: list[dict]) -> None:
        """One day, fetched completely: its totals and every photo viewed that day."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO account_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (day, totals.get("total"), totals.get("photos"), totals.get("photostream"),
                             totals.get("sets"), totals.get("collections"), totals.get("galleries"), int(time.time())))
            self.db.execute("DELETE FROM photo_daily WHERE day=?", (day,))
            self.db.executemany("INSERT OR REPLACE INTO photo_daily VALUES (:photo_id, :day, :views, :comments, "
                                ":favorites)", [{**p, "day": day} for p in photos])
            self.db.execute("INSERT OR REPLACE INTO stats_days VALUES (?, 'done', ?, ?)",
                            (day, len(photos), int(time.time())))

    def mark_stats_day(self, day: str, status: str) -> None:
        """'none' when Flickr has no stats that far back (where backfill stops)."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO stats_days VALUES (?, ?, 0, ?)", (day, status, int(time.time())))

    def stats_days(self) -> dict[str, str]:
        return {r[0]: r[1] for r in self.db.execute("SELECT day, status FROM stats_days")}

    def stats_fetched_at(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.execute("SELECT day, fetched_at FROM stats_days")}

    def daily_totals(self, since: str | None = None) -> list[dict]:
        sql = "SELECT * FROM account_daily" + (" WHERE day >= ?" if since else "") + " ORDER BY day"
        return [dict(r) for r in self.db.execute(sql, (since,) if since else ())]

    def top_photos(self, since: str, until: str | None = None, limit: int = 20, by: str = "views") -> list[dict]:
        if by not in ("views", "comments", "favorites"):
            raise ValueError(by)
        until = until or "9999-12-31"
        return [dict(r) for r in self.db.execute(
            f"SELECT d.photo_id, p.title, SUM(d.views) views, SUM(d.comments) comments, SUM(d.favorites) favorites "
            f"FROM photo_daily d LEFT JOIN photos p ON p.id = d.photo_id WHERE d.day BETWEEN ? AND ? "
            f"GROUP BY d.photo_id ORDER BY SUM(d.{by}) DESC LIMIT ?", (since, until, limit))]

    def photo_history(self, photo_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT day, views, comments, favorites FROM photo_daily WHERE photo_id=? ORDER BY day", (photo_id,))]
