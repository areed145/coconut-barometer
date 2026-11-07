"""Apple Music listening history (apple_music.py).

  music_tracks   each track seen, as Apple described it then; enriched: when the catalog filled in an imported one
  music_plays    each play. source 'hourly': an estimated start, the read that found it and the read before (it began
                 after that). source 'export': from Apple's data export (apple_export.py), with the exact start, how
                 long was heard, how it ended, and what it was played from
  mb_*           what MusicBrainz says about them (musicbrainz.py): each track asked once, found or not, and each
                 artist's facts (type, origin, years, genres, tags)
  artist_profiles  what Claude says about each artist with over 10 plays: scenes, styles, decades, country (artist_profiles.py)"""
from __future__ import annotations

import json

from .db import Database

TRACK_COLUMNS = ("id", "kind", "name", "artist", "album", "genres", "duration_ms", "release_date", "isrc", "artwork", "url", "bg_color", "text_color", "composer")
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
        """Tracks with a catalog number the catalog hasn't been asked about (or asked before composers were kept)."""
        return [r[0] for r in self.db.execute("SELECT id FROM music_tracks WHERE (enriched IS NULL OR composer IS NULL) "
                                              "AND regexp_full_match(id, '[0-9]+') ORDER BY id LIMIT ?", (limit,))]

    def music_enrich(self, ids: list[str], tracks: list[dict], now: int) -> None:
        """What the catalog said about them. Ones it didn't know are marked asked, so they aren't asked for ever."""
        with self.db:
            for t in tracks:
                self.db.execute("UPDATE music_tracks SET name=?, artist=?, album=?, genres=?, duration_ms=?, release_date=?, "
                                "isrc=?, artwork=?, url=?, bg_color=?, text_color=?, composer=? WHERE id=?", (*[t[c] for c in TRACK_COLUMNS[2:]], t["id"]))
            marks = ", ".join("?" * len(ids))
            self.db.execute(f"UPDATE music_tracks SET enriched=?, composer=COALESCE(composer, '') WHERE id IN ({marks})", (now, *ids))

    def music_plays(self, since: int) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT p.track_id, p.played_at, p.seen_at, p.window_start, COALESCE(p.listened_ms, t.duration_ms) AS ms, t.name, "
            "t.artist, t.album, t.genres, t.url, t.artwork IS NOT NULL AS has_art, t.bg_color, t.text_color FROM music_plays p JOIN music_tracks t ON t.id = p.track_id "
            "WHERE p.played_at >= ? ORDER BY p.played_at", (since,))]

    def music_between(self, start: int, end: int) -> list[dict]:
        return [r for r in self.music_plays(start) if r["played_at"] < end]

    def music_graph_plays(self) -> list[dict]:
        """Every play in order, with what the artist graph reads."""
        return [dict(r) for r in self.db.execute(
            "SELECT p.played_at, COALESCE(p.listened_ms, t.duration_ms) AS ms, t.name, t.artist, t.genres, t.composer FROM music_plays p "
            "JOIN music_tracks t ON t.id = p.track_id WHERE t.artist <> '' ORDER BY p.played_at")]

    def music_recent(self, limit: int) -> list[dict]:
        """The newest plays, each track once (at its latest play), newest first."""
        return [dict(r) for r in self.db.execute(
            "SELECT p.track_id, MAX(p.played_at) AS played_at, ANY_VALUE(t.name) AS name, ANY_VALUE(t.artist) AS artist, "
            "ANY_VALUE(t.album) AS album, BOOL_OR(t.artwork IS NOT NULL) AS has_art, ANY_VALUE(t.bg_color) AS bg_color "
            "FROM music_plays p JOIN music_tracks t ON t.id = p.track_id GROUP BY p.track_id ORDER BY played_at DESC LIMIT ?", (limit,))]

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

    # ---- MusicBrainz (musicbrainz.py) ---------------------------------------------------------------------
    _MB_PLAYS = "LEFT JOIN (SELECT track_id, COUNT(*) AS n FROM music_plays GROUP BY track_id) p ON p.track_id = {}"

    def mb_to_ask(self, limit: int) -> list[dict]:
        """Tracks MusicBrainz hasn't been asked about, most played first. One Apple's catalog will still fill in (its
        ISRC, from an imported track) waits for that."""
        return [dict(r) for r in self.db.execute(
            f"SELECT t.id, t.name, t.artist, t.isrc FROM music_tracks t {self._MB_PLAYS.format('t.id')} "
            "WHERE t.artist <> '' AND t.id NOT IN (SELECT track_id FROM mb_recordings) "
            "AND (t.isrc IS NOT NULL OR t.enriched IS NOT NULL OR NOT regexp_full_match(t.id, '[0-9]+')) "
            "ORDER BY COALESCE(p.n, 0) DESC, t.id LIMIT ?", (limit,))]

    def mb_save_track(self, track_id: str, recording: str | None, how: str, credits: list[dict], now: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM mb_credits WHERE track_id = ?", (track_id,))
            for c in credits:
                self.db.execute("INSERT INTO mb_credits (track_id, role, name, mbid) VALUES (?, ?, ?, ?)",
                                (track_id, c["role"], c["name"], c["mbid"]))
            self.db.execute("INSERT OR REPLACE INTO mb_recordings (track_id, recording, how, asked) VALUES (?, ?, ?, ?)",
                            (track_id, recording, how, now))

    def mb_artists_to_ask(self, limit: int) -> list[dict]:
        """The artists your tracks are credited to (main or featured) not looked up yet, or looked up before their
        facts were kept, most played first."""
        return [dict(r) for r in self.db.execute(
            f"SELECT c.mbid, any_value(c.name) AS name FROM mb_credits c {self._MB_PLAYS.format('c.track_id')} "
            "WHERE c.role IN ('artist', 'featured') AND c.mbid NOT IN (SELECT mbid FROM mb_artists WHERE profiled IS NOT NULL) "
            "GROUP BY c.mbid ORDER BY SUM(COALESCE(p.n, 0)) DESC, c.mbid LIMIT ?", (limit,))]

    def mb_save_artist(self, mbid: str, name: str, rels: list[dict], now: int, facts: dict | None = None) -> None:
        """facts: musicbrainz.artist_facts (type, country, area, begin_year, end_year, genres, tags)."""
        f = facts or {}
        with self.db:
            self.db.execute("DELETE FROM mb_artist_rels WHERE mbid = ?", (mbid,))
            for r in rels:
                self.db.execute("INSERT INTO mb_artist_rels (mbid, role, other, other_name) VALUES (?, ?, ?, ?)",
                                (mbid, r["role"], r["mbid"], r["name"]))
            self.db.execute("INSERT OR REPLACE INTO mb_artists (mbid, name, asked, type, country, area, begin_year, end_year, "
                            "genres, tags, profiled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (mbid, name, now, f.get("type"), f.get("country"), f.get("area"), f.get("begin_year"),
                             f.get("end_year"), json.dumps(f.get("genres") or []), json.dumps(f.get("tags") or []), now))

    def mb_graph(self) -> dict:
        """What the artist graph reads: each (track artist, role, credited name, MBID) with its track count, and the
        artists' relations."""
        credits = [dict(r) for r in self.db.execute(
            "SELECT t.artist, c.role, c.name, c.mbid, COUNT(*) AS tracks FROM mb_credits c JOIN music_tracks t ON t.id = c.track_id "
            "GROUP BY t.artist, c.role, c.name, c.mbid")]
        rels = [dict(r) for r in self.db.execute(
            "SELECT r.mbid, a.name, r.role, r.other, r.other_name FROM mb_artist_rels r JOIN mb_artists a ON a.mbid = r.mbid")]
        artists = {r["mbid"]: {**dict(r), "genres": json.loads(r["genres"] or "[]"), "tags": json.loads(r["tags"] or "[]")}
                   for r in self.db.execute("SELECT mbid, type, country, area, begin_year, end_year, genres, tags "
                                            "FROM mb_artists WHERE profiled IS NOT NULL")}
        return {"credits": credits, "rels": rels, "artists": artists}

    def mb_progress(self) -> dict:
        r = self.db.execute("SELECT COUNT(*), COUNT(recording) FROM mb_recordings").fetchone()
        total = self.db.execute("SELECT COUNT(*) FROM music_tracks WHERE artist <> ''").fetchone()[0]
        artists = self.db.execute("SELECT COUNT(*) FROM mb_artists").fetchone()[0]
        return {"asked": r[0], "found": r[1], "tracks": total, "artists": artists}

    # ---- artist profiles (artist_profiles.py) -------------------------------------------------------------
    def music_artist_plays(self) -> list[tuple[str, int]]:
        """(track artist, plays), most played first: credited() turns these into main artists."""
        return [tuple(r) for r in self.db.execute(
            "SELECT t.artist, COUNT(*) AS n FROM music_plays p JOIN music_tracks t ON t.id = p.track_id "
            "WHERE t.artist <> '' GROUP BY t.artist ORDER BY n DESC, t.artist")]

    def profiles(self) -> dict[str, dict]:
        """artist -> what Claude said: scenes, styles, decades (lists), country. An artist it didn't know has empty ones."""
        return {r["artist"]: {"scenes": json.loads(r["scenes"] or "[]"), "styles": json.loads(r["styles"] or "[]"),
                              "decades": json.loads(r["decades"] or "[]"), "country": r["country"]}
                for r in self.db.execute("SELECT artist, scenes, styles, decades, country FROM artist_profiles")}

    def profiles_save(self, rows: list[dict], model: str, now: int) -> None:
        with self.db:
            for r in rows:
                self.db.execute("INSERT OR REPLACE INTO artist_profiles (artist, scenes, styles, decades, country, model, asked) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)", (r["artist"], json.dumps(r["scenes"]), json.dumps(r["styles"]),
                                                                json.dumps(r["decades"]), r["country"], model, now))

    def music_graph_version(self) -> tuple:
        """Changes whenever what the artist graph is made from does: plays, tracks, MusicBrainz's answers, profiles.
        The explorer (music_explore.py) rebuilds its whole graph when it does."""
        return tuple(self.db.execute(
            "SELECT (SELECT COUNT(*) FROM music_plays), (SELECT MAX(played_at) FROM music_plays), "
            "(SELECT COUNT(*) FROM music_tracks), (SELECT MAX(enriched) FROM music_tracks), "
            "(SELECT COUNT(*) FROM mb_recordings), (SELECT MAX(asked) FROM mb_recordings), "
            "(SELECT COUNT(*) FROM mb_artists), (SELECT MAX(asked) FROM mb_artists), "
            "(SELECT COUNT(*) FROM artist_profiles), (SELECT MAX(asked) FROM artist_profiles)").fetchone())

    def profiles_clear(self) -> None:
        self.db.execute("DELETE FROM artist_profiles")
