"""Per-day stats from Flickr's stats API: views/comments/faves per photo per day
(photo_daily), the account's daily totals (account_daily), and which days are done."""
from __future__ import annotations

import time

from .db import Database


class StatsStore(Database):
    def save_stats_day(self, day: str, totals: dict, photos: list[dict], status: str = "done") -> None:
        """One day: its totals and every photo viewed that day. status "partial": today, so far."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO account_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (day, totals.get("total"), totals.get("photos"), totals.get("photostream"),
                             totals.get("sets"), totals.get("collections"), totals.get("galleries"), int(time.time())))
            rows = list({p["photo_id"]: {**p, "day": day} for p in photos}.values())
            have = [r[0] for r in self.db.execute("SELECT photo_id FROM photo_daily WHERE day=?", (day,))]
            self.db.executemany("DELETE FROM photo_daily WHERE day=? AND photo_id=?",
                                [(day, i) for i in have if i not in {r["photo_id"] for r in rows}])
            self.db.executemany("INSERT INTO photo_daily VALUES (:photo_id, :day, :views, :comments, :favorites) "
                                "ON CONFLICT(photo_id, day) DO UPDATE SET views=excluded.views, "
                                "comments=excluded.comments, favorites=excluded.favorites", rows)
            self.db.execute("INSERT OR REPLACE INTO stats_days VALUES (?, ?, ?, ?)",
                            (day, status, len(photos), int(time.time())))

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
            f"SELECT d.photo_id, p.title, SUM(d.views) AS views, SUM(d.comments) AS comments, SUM(d.favorites) AS favorites "
            f"FROM photo_daily d LEFT JOIN photos p ON p.id = d.photo_id WHERE d.day BETWEEN ? AND ? "
            f"GROUP BY d.photo_id, p.title ORDER BY SUM(d.{by}) DESC, d.photo_id LIMIT ?", (since, until, limit))]

    def photo_history(self, photo_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT day, views, comments, favorites FROM photo_daily WHERE photo_id=? ORDER BY day", (photo_id,))]
