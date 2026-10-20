"""Your library as last synced: photos, albums, which photo is in which album,
plus local edits made when changes are applied."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Iterable

from .db import Database

PHOTO_COLS = ["id", "title", "description", "tags", "lat", "lon", "accuracy", "media", "thumb_url",
              "secret", "server", "date_taken", "date_upload", "last_update", "ispublic",
              "date_taken_unknown", "views", "faves", "comments", "license", "width", "height",
              "url_l", "url_h", "url_k"]
ALBUM_COLS = ["id", "title", "count", "date_update", "cover_url", "description"]


class LibraryStore(Database):
    def replace_library(self, photos: list[dict], albums: list[dict], memberships: Iterable[tuple[str, str]]):
        """Full sync: the whole library as just fetched."""
        with self.db:
            self.db.execute("DELETE FROM photos")
            self.db.execute("DELETE FROM albums")
            self.db.execute("DELETE FROM memberships")
            self._write_photos(photos)
            self._write_albums(albums)
            self.db.executemany("INSERT OR IGNORE INTO memberships VALUES (?, ?)", memberships)
            self._snapshot()
        self.set_meta("last_sync", int(time.time()))

    def upsert_photos(self, photos: list[dict]) -> None:
        """Incremental sync: add new photos, update changed ones."""
        with self.db:
            self._write_photos(photos)

    def delete_photos(self, photo_ids: Iterable[str]) -> None:
        ids = [(i,) for i in photo_ids]
        with self.db:
            self.db.executemany("DELETE FROM photos WHERE id=?", ids)
            self.db.executemany("DELETE FROM memberships WHERE photo_id=?", ids)

    def photo_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT id FROM photos")}

    def upsert_albums(self, albums: list[dict]) -> None:
        with self.db:
            self._write_albums(albums)

    def delete_albums(self, album_ids: Iterable[str]) -> None:
        ids = [(i,) for i in album_ids]
        with self.db:
            self.db.executemany("DELETE FROM albums WHERE id=?", ids)
            self.db.executemany("DELETE FROM memberships WHERE album_id=?", ids)

    def replace_album_members(self, album_id: str, photo_ids: Iterable[str]) -> None:
        with self.db:
            self.db.execute("DELETE FROM memberships WHERE album_id=?", (album_id,))
            self.db.executemany("INSERT OR IGNORE INTO memberships VALUES (?, ?)", [(album_id, p) for p in photo_ids])

    def snapshot_counts(self) -> None:
        """Today's (UTC) views/faves/comments for every photo, one row per photo per day."""
        with self.db:
            self._snapshot()

    def _snapshot(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        self.db.execute("INSERT OR REPLACE INTO photo_stats SELECT id, ?, views, faves, comments "
                        "FROM photos WHERE views IS NOT NULL", (day,))

    def _write_photos(self, photos: list[dict]) -> None:
        self.db.executemany(
            f"INSERT INTO photos ({', '.join(PHOTO_COLS)}) VALUES ({', '.join(':' + c for c in PHOTO_COLS)}) "
            f"ON CONFLICT(id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in PHOTO_COLS if c != 'id')}",
            [{c: p.get(c) for c in PHOTO_COLS} for p in photos])

    def _write_albums(self, albums: list[dict]) -> None:
        self.db.executemany(
            f"INSERT INTO albums ({', '.join(ALBUM_COLS)}) VALUES ({', '.join(':' + c for c in ALBUM_COLS)}) "
            f"ON CONFLICT(id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in ALBUM_COLS if c != 'id')}",
            [{c: a.get(c) for c in ALBUM_COLS} for a in albums])

    def photos(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM photos ORDER BY date_upload DESC").fetchall()

    def photo(self, photo_id: str):
        return self.db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()

    def albums(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM albums ORDER BY title COLLATE NOCASE").fetchall()

    def add_album(self, album_id: str, title: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO albums (id, title, count) VALUES (?, ?, 0)", (album_id, title))

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

    def album_id(self, title: str) -> str | None:
        r = self.db.execute("SELECT id FROM albums WHERE title=?", (title,)).fetchone()
        return r[0] if r else None
