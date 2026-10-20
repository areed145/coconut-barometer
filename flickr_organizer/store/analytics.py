"""Analytics over the per-day stats, snapshots, faves and comments.

photo_daily / account_daily are Flickr's own per-day numbers (GMT days); faves and
comments carry their own dates, so their timelines are exact too.
"""
from __future__ import annotations

from .db import Database

BREAKDOWNS = {
    "album": ("JOIN memberships m ON m.photo_id = d.photo_id JOIN albums a ON a.id = m.album_id", "a.title"),
    "camera": ("LEFT JOIN cameras c ON c.photo_id = d.photo_id", "COALESCE(NULLIF(c.camera, ''), '(unknown)')"),
    "year": ("JOIN photos p ON p.id = d.photo_id", "COALESCE(substr(p.date_taken, 1, 4), '(unknown)')"),
    "media": ("JOIN photos p ON p.id = d.photo_id", "p.media"),
}


class AnalyticsStore(Database):
    def daily_series(self, since: str) -> list[dict]:
        """Per day: total views (account_daily), faves and comments received (by their own dates)."""
        views = {r["day"]: dict(r) for r in self.db.execute(
            "SELECT day, total, photos, photostream, sets FROM account_daily WHERE day >= ?", (since,))}
        faves = dict(self.db.execute("SELECT date(date_faved, 'unixepoch') d, COUNT(*) FROM faves "
                                     "WHERE date(date_faved, 'unixepoch') >= ? GROUP BY d", (since,)).fetchall())
        comments = dict(self.db.execute("SELECT date(date_created, 'unixepoch') d, COUNT(*) FROM comments "
                                        "WHERE date(date_created, 'unixepoch') >= ? GROUP BY d", (since,)).fetchall())
        days = sorted(set(views) | set(faves) | set(comments))
        return [{"day": d, "views": (views.get(d) or {}).get("total"), "faves": faves.get(d, 0),
                 "comments": comments.get(d, 0)} for d in days]

    def totals(self) -> dict:
        r = self.db.execute("SELECT COUNT(*) photos, SUM(views) views, SUM(faves) faves, SUM(comments) comments "
                            "FROM photos").fetchone()
        return dict(r)

    def gainers(self, days: int, today: str, limit: int = 20) -> list[dict]:
        """Views in the last `days` days against the `days` before them."""
        return [dict(r) for r in self.db.execute(
            "WITH w AS (SELECT photo_id, "
            "  SUM(CASE WHEN day > date(?, ?) THEN views ELSE 0 END) recent, "
            "  SUM(CASE WHEN day <= date(?, ?) THEN views ELSE 0 END) before "
            "  FROM photo_daily WHERE day > date(?, ?) GROUP BY photo_id) "
            "SELECT w.photo_id, p.title, w.recent, w.before, w.recent - w.before gain FROM w "
            "LEFT JOIN photos p ON p.id = w.photo_id ORDER BY gain DESC LIMIT ?",
            (today, f"-{days} day", today, f"-{days} day", today, f"-{2 * days} day", limit))]

    def breakdown(self, by: str, since: str, limit: int = 30) -> list[dict]:
        if by not in BREAKDOWNS:
            raise ValueError(by)
        join, key = BREAKDOWNS[by]
        return [dict(r) for r in self.db.execute(
            f"SELECT {key} label, SUM(d.views) views, SUM(d.favorites) faves, SUM(d.comments) comments, "
            f"COUNT(DISTINCT d.photo_id) photos FROM photo_daily d {join} WHERE d.day >= ? "
            f"GROUP BY label ORDER BY views DESC LIMIT ?", (since, limit))]

    def photo_timeline(self, photo_id: str) -> dict:
        q = lambda sql: [dict(r) for r in self.db.execute(sql, (photo_id,))]
        return {
            "daily": q("SELECT day, views, comments, favorites FROM photo_daily WHERE photo_id=? ORDER BY day"),
            "snapshots": q("SELECT day, views, faves, comments FROM photo_stats WHERE photo_id=? ORDER BY day"),
            "faves": q("SELECT date(date_faved, 'unixepoch') day, COUNT(*) n FROM faves WHERE photo_id=? "
                       "GROUP BY day ORDER BY day"),
        }
