"""Local SQLite state: a cache of your library plus everything the tool has decided.

Tables
  photos           mirror of your photos (refreshed on every sync)
  albums           mirror of your albums
  memberships      which photo is in which album (refreshed on every sync)
  classifications  cached vision results, keyed by photo + model/prompt fingerprint
  cameras          each photo's camera, lens and full EXIF (JSON), fetched once
  photo_stats      daily snapshot of each photo's views / faves / comments (one per sync day)
  account_stats    daily snapshot of your account's total views (flickr.stats.getTotalViews)
  flickr_groups    groups you belong to; group_photos: which of your photos are in each pool
  comments, faves  comments on your photos and who faved them (fetched only where counts changed)
  social_fetched   the count each photo had when its comments/faves were last fetched
  queue            per-photo proposed changes for the review tool:
                   pending (to review) | none (nothing to change) | applied | rejected | failed
  decisions        per (photo, action, target): applied / rejected / failed.
                   Rejected items are never proposed again.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id TEXT PRIMARY KEY,
    title TEXT, description TEXT, tags TEXT,
    lat REAL, lon REAL, accuracy INTEGER,
    media TEXT, thumb_url TEXT, secret TEXT, server TEXT,
    date_taken TEXT, date_upload INTEGER, last_update INTEGER,
    ispublic INTEGER, date_taken_unknown INTEGER,
    views INTEGER, faves INTEGER, comments INTEGER, license TEXT, width INTEGER, height INTEGER
);
CREATE TABLE IF NOT EXISTS albums (
    id TEXT PRIMARY KEY, title TEXT, count INTEGER
);
CREATE TABLE IF NOT EXISTS memberships (
    album_id TEXT, photo_id TEXT, PRIMARY KEY (album_id, photo_id)
);
CREATE INDEX IF NOT EXISTS memberships_photo ON memberships(photo_id);
CREATE TABLE IF NOT EXISTS classifications (
    photo_id TEXT, fingerprint TEXT, result TEXT, created_at INTEGER,
    PRIMARY KEY (photo_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS decisions (
    photo_id TEXT, action TEXT, target TEXT, status TEXT, detail TEXT, at INTEGER,
    PRIMARY KEY (photo_id, action, target)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS photo_stats (
    photo_id TEXT, day TEXT, views INTEGER, faves INTEGER, comments INTEGER,
    PRIMARY KEY (photo_id, day)
);
CREATE TABLE IF NOT EXISTS account_stats (day TEXT PRIMARY KEY, stats TEXT);
CREATE TABLE IF NOT EXISTS flickr_groups (
    id TEXT PRIMARY KEY, name TEXT, path_alias TEXT, members INTEGER, pool_count INTEGER,
    privacy TEXT, info TEXT, fetched_at INTEGER
);
CREATE TABLE IF NOT EXISTS group_photos (
    group_id TEXT, photo_id TEXT, date_added INTEGER, PRIMARY KEY (group_id, photo_id)
);
CREATE INDEX IF NOT EXISTS group_photos_photo ON group_photos(photo_id);
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY, photo_id TEXT, author TEXT, author_name TEXT, real_name TEXT,
    date_created INTEGER, permalink TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS comments_photo ON comments(photo_id);
CREATE TABLE IF NOT EXISTS faves (
    photo_id TEXT, user_id TEXT, username TEXT, real_name TEXT, date_faved INTEGER,
    PRIMARY KEY (photo_id, user_id)
);
CREATE TABLE IF NOT EXISTS social_fetched (
    photo_id TEXT, kind TEXT, count INTEGER, fetched_at INTEGER, PRIMARY KEY (photo_id, kind)
);
CREATE TABLE IF NOT EXISTS queue (
    photo_id TEXT PRIMARY KEY, status TEXT, changes TEXT, built_at INTEGER,
    reviewed_at INTEGER, result TEXT
);
CREATE INDEX IF NOT EXISTS queue_status ON queue(status);
CREATE TABLE IF NOT EXISTS cameras (
    photo_id TEXT PRIMARY KEY, camera TEXT, make TEXT, model TEXT, fetched_at INTEGER,
    lens TEXT, exif TEXT
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        # The review tool's background worker opens its own connection to the same file;
        # WAL lets the page keep reading while it writes, and the timeout waits out locks.
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(photos)")}
        added = [("date_taken_unknown", "INTEGER"), ("views", "INTEGER"), ("faves", "INTEGER"),
                 ("comments", "INTEGER"), ("license", "TEXT"), ("width", "INTEGER"), ("height", "INTEGER")]
        for col, kind in added:   # databases from before these columns existed
            if col not in cols:
                with self.db:
                    self.db.execute(f"ALTER TABLE photos ADD COLUMN {col} {kind}")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(cameras)")}
        for col in ("lens", "exif"):
            if col not in cols:
                with self.db:
                    self.db.execute(f"ALTER TABLE cameras ADD COLUMN {col} TEXT")

    # ---- meta -------------------------------------------------------------
    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

    # ---- library mirror ---------------------------------------------------
    def replace_library(self, photos: list[dict], albums: list[dict], memberships: Iterable[tuple[str, str]]):
        with self.db:
            self.db.execute("DELETE FROM photos")
            self.db.execute("DELETE FROM albums")
            self.db.execute("DELETE FROM memberships")
            cols = ["id", "title", "description", "tags", "lat", "lon", "accuracy", "media", "thumb_url",
                    "secret", "server", "date_taken", "date_upload", "last_update", "ispublic",
                    "date_taken_unknown", "views", "faves", "comments", "license", "width", "height"]
            self.db.executemany(
                f"INSERT INTO photos ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})",
                [{c: p.get(c) for c in cols} for p in photos],
            )
            # One snapshot of the counts per day, so you can see how photos grow over time.
            day = time.strftime("%Y-%m-%d")
            self.db.execute("INSERT OR REPLACE INTO photo_stats SELECT id, ?, views, faves, comments "
                            "FROM photos WHERE views IS NOT NULL", (day,))
            self.db.executemany("INSERT INTO albums VALUES (:id,:title,:count)", albums)
            self.db.executemany("INSERT OR IGNORE INTO memberships VALUES (?, ?)", memberships)
        self.set_meta("last_sync", int(time.time()))

    def photos(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM photos ORDER BY date_upload DESC").fetchall()

    def photo(self, photo_id: str):
        return self.db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()

    def albums(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM albums ORDER BY title COLLATE NOCASE").fetchall()

    def add_album(self, album_id: str, title: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO albums VALUES (?, ?, 0)", (album_id, title))

    def memberships(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for r in self.db.execute("SELECT photo_id, album_id FROM memberships"):
            out.setdefault(r["photo_id"], set()).add(r["album_id"])
        return out

    def add_membership(self, album_id: str, photo_id: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO memberships VALUES (?, ?)", (album_id, photo_id))

    def set_photo_albums(self, photo_id: str, album_ids: list[str]) -> None:
        with self.db:
            self.db.execute("DELETE FROM memberships WHERE photo_id=?", (photo_id,))
            self.db.executemany("INSERT OR IGNORE INTO memberships VALUES (?, ?)", [(a, photo_id) for a in album_ids])

    def set_title_local(self, photo_id: str, title: str) -> None:
        with self.db:
            self.db.execute("UPDATE photos SET title=? WHERE id=?", (title, photo_id))

    def set_description_local(self, photo_id: str, description: str) -> None:
        with self.db:
            self.db.execute("UPDATE photos SET description=? WHERE id=?", (description, photo_id))

    def add_tags_local(self, photo_id: str, tags: list[str]) -> None:
        row = self.photo(photo_id)
        if not row:
            return
        existing = (row["tags"] or "").split()
        merged = existing + [t for t in tags if t not in existing]
        with self.db:
            self.db.execute("UPDATE photos SET tags=? WHERE id=?", (" ".join(merged), photo_id))

    def update_photo(self, photo_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE photos SET {cols} WHERE id=?", (*fields.values(), photo_id))

    # ---- engagement ---------------------------------------------------------
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

    # ---- review queue -----------------------------------------------------
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

    # ---- cameras ----------------------------------------------------------
    def cameras(self) -> dict[str, str]:
        return {r[0]: r[1] for r in self.db.execute("SELECT photo_id, camera FROM cameras")}

    def camera_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT photo_id FROM cameras")}

    def save_camera(self, photo_id: str, camera: str, make: str, model: str,
                    lens: str = "", exif: dict | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO cameras (photo_id, camera, make, model, lens, exif, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (photo_id, camera, make, model, lens, json.dumps(exif or {}), int(time.time())))

    # ---- classifications --------------------------------------------------
    def classification(self, photo_id: str, fingerprint: str) -> dict | None:
        row = self.db.execute(
            "SELECT result FROM classifications WHERE photo_id=? AND fingerprint=?", (photo_id, fingerprint)
        ).fetchone()
        return json.loads(row["result"]) if row else None

    def classified_at(self, photo_id: str, fingerprint: str) -> int | None:
        r = self.db.execute("SELECT created_at FROM classifications WHERE photo_id=? AND fingerprint=?",
                            (photo_id, fingerprint)).fetchone()
        return r[0] if r else None

    def classified_ids(self, fingerprint: str) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT photo_id FROM classifications WHERE fingerprint=?", (fingerprint,))}

    def save_classification(self, photo_id: str, fingerprint: str, result: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO classifications VALUES (?, ?, ?, ?)",
                (photo_id, fingerprint, json.dumps(result), int(time.time())),
            )

    # ---- decisions --------------------------------------------------------
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

    def counts(self) -> dict[str, int]:
        q = lambda sql: self.db.execute(sql).fetchone()[0]
        return {
            "photos": q("SELECT COUNT(*) FROM photos"),
            "geotagged": q("SELECT COUNT(*) FROM photos WHERE lat IS NOT NULL"),
            "albums": q("SELECT COUNT(*) FROM albums"),
            "classified": q("SELECT COUNT(DISTINCT photo_id) FROM classifications"),
            "cameras": q("SELECT COUNT(*) FROM cameras"),
            "to review": q("SELECT COUNT(*) FROM queue WHERE status='pending'"),
            "applied": q("SELECT COUNT(*) FROM decisions WHERE status='applied'"),
            "rejected": q("SELECT COUNT(*) FROM decisions WHERE status='rejected'"),
            "failed": q("SELECT COUNT(*) FROM decisions WHERE status='failed'"),
        }
