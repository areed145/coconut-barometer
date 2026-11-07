"""Apple Music listening history (apple_music.py).

  music_tracks   each track seen, as Apple described it then; enriched: when the catalog filled in an imported one
  music_plays    each play. source 'hourly': an estimated start, the read that found it and the read before (it began
                 after that). source 'export': from Apple's data export (apple_export.py), with the exact start, how
                 long was heard, how it ended, and what it was played from"""
from __future__ import annotations

from .db import Database

TRACK_COLUMNS = ("id", "kind", "name", "artist", "album", "genres", "duration_ms", "release_date", "isrc", "artwork", "url", "bg_color", "text_color")
EXPORT_COLUMNS = ("{started: 'BIGINT', track_id: 'VARCHAR', song: 'VARCHAR', artist: 'VARCHAR', album: 'VARCHAR', "
                  "duration_ms: 'BIGINT', listened_ms: 'BIGINT', end_reason: 'VARCHAR', utc_offset: 'BIGINT', "
                  "container_type: 'VARCHAR', feature: 'VARCHAR', match: 'VARCHAR'}")


class MusicStore(Database):
    def music_save(self, tracks: list[dict], plays: list[tuple], snapshot: str, now: int, missed: int) -> None:
        """One read, all or nothing, so a crash between the two can't count the same plays again."""
        total = int(self.get_meta("music_missed") or 0) + missed
        first = self.get_meta("music_first_read") is None
        marks = ", ".join("?" * len(TRACK_COLUMNS))
        with self.db:
            for t in tracks:
                self.db.execute(f"INSERT OR IGNORE INTO music_tracks ({', '.join(TRACK_COLUMNS)}, first_seen) VALUES ({marks}, ?)",
                                (*[t[c] for c in TRACK_COLUMNS], now))
            for p in plays:
                self.db.execute("INSERT INTO music_plays (track_id, played_at, seen_at, window_start, source) "
                                "VALUES (?, ?, ?, ?, 'hourly')", p)
            self.set_meta("music_snapshot", snapshot)
            self.set_meta("music_last_ok", now)
            self.set_meta("music_missed", total)
            if first:
                self.set_meta("music_first_read", now)

    def music_import(self, path: str) -> int:
        """The small file from apple_export.slim, replacing any imported before. Up to the first hourly read only:
        after that the reads have it. Read by DuckDB itself: it's over 100,000 rows."""
        before = int(self.get_meta("music_first_read") or 2 ** 62)
        reader = (f"read_csv(?, header=true, auto_detect=false, delim=',', quote='\"', escape='\"', compression='gzip', "
                  f"columns={EXPORT_COLUMNS})")
        with self.db:
            self.db.execute("DELETE FROM music_plays WHERE source = 'export'")
            self.db.execute(
                "INSERT OR IGNORE INTO music_tracks (id, kind, name, artist, album, genres, duration_ms, first_seen) "
                "SELECT track_id, 'export', any_value(song), any_value(artist), mode(album), '[]', max(duration_ms), min(started) "
                f"FROM {reader} GROUP BY track_id", (path,))
            self.db.execute(
                "INSERT INTO music_plays (track_id, played_at, seen_at, source, listened_ms, end_reason, context) "
                f"SELECT track_id, started, started, 'export', listened_ms, end_reason, COALESCE(NULLIF(feature, ''), container_type) "
                f"FROM {reader} WHERE started < ?", (path, before))
        return self.db.execute("SELECT COUNT(*) FROM music_plays WHERE source = 'export'").fetchone()[0]

    def music_imported(self) -> dict:
        r = self.db.execute("SELECT COUNT(*), MAX(played_at) FROM music_plays WHERE source = 'export'").fetchone()
        return {"plays": r[0], "until": r[1]}

    def music_to_enrich(self, limit: int) -> list[str]:
        """Tracks with a catalog number the catalog hasn't been asked about."""
        return [r[0] for r in self.db.execute("SELECT id FROM music_tracks WHERE enriched IS NULL "
                                              "AND regexp_full_match(id, '[0-9]+') ORDER BY id LIMIT ?", (limit,))]

    def music_enrich(self, ids: list[str], tracks: list[dict], now: int) -> None:
        """What the catalog said about them. Ones it didn't know are marked asked, so they aren't asked for ever."""
        with self.db:
            for t in tracks:
                self.db.execute("UPDATE music_tracks SET name=?, artist=?, album=?, genres=?, duration_ms=?, release_date=?, "
                                "isrc=?, artwork=?, url=?, bg_color=?, text_color=? WHERE id=?", (*[t[c] for c in TRACK_COLUMNS[2:]], t["id"]))
            marks = ", ".join("?" * len(ids))
            self.db.execute(f"UPDATE music_tracks SET enriched=? WHERE id IN ({marks})", (now, *ids))

    def music_plays(self, since: int) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT p.track_id, p.played_at, p.seen_at, p.window_start, COALESCE(p.listened_ms, t.duration_ms) AS ms, t.name, "
            "t.artist, t.album, t.genres, t.url, t.artwork IS NOT NULL AS has_art, t.bg_color, t.text_color FROM music_plays p JOIN music_tracks t ON t.id = p.track_id "
            "WHERE p.played_at >= ? ORDER BY p.played_at", (since,))]

    def music_between(self, start: int, end: int) -> list[dict]:
        return [r for r in self.music_plays(start) if r["played_at"] < end]

    def music_artwork(self, track_id: str) -> str | None:
        r = self.db.execute("SELECT artwork FROM music_tracks WHERE id = ?", (track_id,)).fetchone()
        return r[0] if r else None

    def music_rain(self, since: int) -> tuple[list[tuple], int | None, int | None]:
        """Every wet minute from `since`, and the time the stations cover."""
        wet = [tuple(r) for r in self.db.execute("SELECT device_id, ts, rain FROM wx_obs WHERE rain > 0 AND ts >= ?", (since,))]
        first, last = self.db.execute("SELECT MIN(ts), MAX(ts) FROM wx_obs").fetchone()
        return wet, first, last

    def music_play_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM music_plays").fetchone()[0]
