"""The site's own visitors (#83; see visitors.py for what's collected and what isn't).

  site_hits   one row per page view (kind 'page'), page view from a bot ('bot') or link preview ('preview'),
              for visitors.keep_days
  site_days   each day's visitors and page views once its raw hits are pruned, so long ranges keep a trend"""
from __future__ import annotations

from .db import Database

PAGE = "kind='page'"


class VisitorsStore(Database):
    def site_hits_add(self, rows: list[tuple]) -> None:
        from ..visitors import COLUMNS

        with self.db:
            self.db.executemany(f"INSERT INTO site_hits ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})", rows)

    def site_hits_prune(self, before_day: str) -> int:
        """Days before `before_day` into site_days (kept if already there), then their raw rows go."""
        with self.db:
            self.db.execute(
                f"INSERT OR IGNORE INTO site_days SELECT day, COUNT(DISTINCT visitor), COUNT(*) FROM site_hits "
                f"WHERE day < ? AND {PAGE} GROUP BY day", (before_day,))
            n = self.db.execute("SELECT COUNT(*) FROM site_hits WHERE day < ?", (before_day,)).fetchone()[0]
            self.db.execute("DELETE FROM site_hits WHERE day < ?", (before_day,))
        return n

    def visitors_series(self, since: str) -> list[dict]:
        """Visitors and page views per day from `since`: raw hits where they're kept, site_days before that."""
        return [dict(r) for r in self.db.execute(
            f"SELECT day, COUNT(DISTINCT visitor) AS visitors, COUNT(*) AS views FROM site_hits "
            f"WHERE day >= ? AND {PAGE} GROUP BY day "
            "UNION ALL SELECT day, visitors, views FROM site_days WHERE day >= ? "
            f"AND day < COALESCE((SELECT MIN(day) FROM site_hits WHERE {PAGE}), '9999') ORDER BY day", (since, since))]

    def visitors_totals(self, since: str) -> dict:
        r = self.db.execute(
            f"SELECT COUNT(DISTINCT visitor) AS visitors, COUNT(DISTINCT session) AS sessions, COUNT(*) AS views "
            f"FROM site_hits WHERE day >= ? AND {PAGE}", (since,)).fetchone()
        out = dict(r)
        out["kinds"] = {k: n for k, n in self.db.execute(
            "SELECT kind, COUNT(*) FROM site_hits WHERE day >= ? GROUP BY kind", (since,))}
        out["now"] = self.db.execute(
            f"SELECT COUNT(DISTINCT visitor) FROM site_hits WHERE ts >= epoch(now())::BIGINT - 300 AND {PAGE}").fetchone()[0]
        return out

    def visitors_top(self, since: str, by: str, limit: int = 20, kind: str = "page") -> list[dict]:
        """The most common values of a column (route, ref_host, city, ...) in the period: views and visitors."""
        # Cities with their region (the country has its own list); regions with their country.
        label = {"city": "concat_ws(', ', city, region)", "region": "concat_ws(', ', region, country)",
                 "utm": "concat_ws(' / ', utm_source, utm_medium, utm_campaign)"}.get(by, by)
        where = {"city": "city IS NOT NULL", "region": "region IS NOT NULL", "utm": "utm_source IS NOT NULL"}.get(by, f"{by} IS NOT NULL")
        return [dict(r) for r in self.db.execute(
            f"SELECT {label} AS label, COUNT(*) AS views, COUNT(DISTINCT visitor) AS visitors FROM site_hits "
            f"WHERE day >= ? AND kind=? AND {where} GROUP BY 1 ORDER BY views DESC, label LIMIT ?", (since, kind, limit))]

    def visitors_items(self, since: str, route: str, limit: int = 12) -> list[dict]:
        """The items opened most on one route (photos for /photos/photo/:id), with their Flickr views."""
        return [dict(r) for r in self.db.execute(
            "SELECT h.item AS photo_id, p.title, p.views AS flickr_views, COUNT(*) AS views, COUNT(DISTINCT h.visitor) AS visitors "
            "FROM site_hits h JOIN photos p ON p.id = h.item WHERE h.day >= ? AND h.kind='page' AND h.route=? "
            "GROUP BY 1, 2, 3 ORDER BY views DESC LIMIT ?", (since, route, limit))]

    def visitors_previews(self, since: str, limit: int = 12) -> list[dict]:
        """Shared links that were unfurled: which photo or album, how often, and by which apps."""
        return [dict(r) for r in self.db.execute(
            "SELECT h.route, h.item, COALESCE(p.title, a.title) AS title, COUNT(*) AS previews, "
            "string_agg(DISTINCT h.browser, ', ') AS apps FROM site_hits h "
            "LEFT JOIN photos p ON h.route = '/p/:id' AND p.id = h.item "
            "LEFT JOIN albums a ON h.route = '/a/:id' AND a.id = h.item "
            "WHERE h.day >= ? AND h.kind='preview' GROUP BY 1, 2, 3 ORDER BY previews DESC LIMIT ?", (since, limit))]

    def visitors_recent(self, limit: int = 30) -> list[dict]:
        """The latest sessions: when, where from, on what, and the pages in order."""
        return [dict(r) for r in self.db.execute(
            "SELECT session, MIN(ts) AS start, MAX(ts) AS last, COUNT(*) AS views, any_value(city) AS city, "
            "any_value(region) AS region, any_value(country) AS country, any_value(device) AS device, "
            "any_value(browser) AS browser, any_value(os) AS os, max(ref_host) AS ref_host, "
            "string_agg(route, ' → ' ORDER BY ts) AS path FROM site_hits WHERE kind='page' "
            "GROUP BY session ORDER BY last DESC LIMIT ?", (limit,))]
