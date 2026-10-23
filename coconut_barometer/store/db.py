"""The DuckDB connection and schema.

The store classes were written for SQLite, so Connection keeps its calling style (execute with
? or :name parameters, rows readable by name or index, `with db:` for a transaction)."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

import duckdb

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS photos (id TEXT PRIMARY KEY, title TEXT, description TEXT, tags TEXT, lat DOUBLE, lon DOUBLE, accuracy INTEGER, media TEXT, thumb_url TEXT, secret TEXT, server TEXT, date_taken TEXT, date_upload BIGINT, last_update BIGINT, ispublic INTEGER, date_taken_unknown INTEGER, views INTEGER, faves INTEGER, comments INTEGER, license TEXT, width INTEGER, height INTEGER, url_l TEXT, url_h TEXT, url_k TEXT, geo_public INTEGER);
CREATE TABLE IF NOT EXISTS albums (id TEXT PRIMARY KEY, title TEXT, count INTEGER, date_update BIGINT, cover_url TEXT, description TEXT);
CREATE TABLE IF NOT EXISTS memberships (album_id TEXT, photo_id TEXT, PRIMARY KEY (album_id, photo_id));
CREATE TABLE IF NOT EXISTS classifications (photo_id TEXT, fingerprint TEXT, result TEXT, created_at BIGINT, PRIMARY KEY (photo_id, fingerprint));
CREATE TABLE IF NOT EXISTS cameras (photo_id TEXT PRIMARY KEY, camera TEXT, make TEXT, model TEXT, fetched_at BIGINT, lens TEXT, exif TEXT);
CREATE TABLE IF NOT EXISTS decisions (photo_id TEXT, action TEXT, target TEXT, status TEXT, detail TEXT, "at" BIGINT, PRIMARY KEY (photo_id, action, target));
CREATE TABLE IF NOT EXISTS queue (photo_id TEXT PRIMARY KEY, status TEXT, changes TEXT, built_at BIGINT, reviewed_at BIGINT, result TEXT);
CREATE TABLE IF NOT EXISTS photo_stats (photo_id TEXT, day TEXT, views INTEGER, faves INTEGER, comments INTEGER, PRIMARY KEY (photo_id, day));
CREATE TABLE IF NOT EXISTS account_stats (day TEXT PRIMARY KEY, stats TEXT);
CREATE TABLE IF NOT EXISTS flickr_groups (id TEXT PRIMARY KEY, name TEXT, path_alias TEXT, members INTEGER, pool_count INTEGER, privacy TEXT, info TEXT, fetched_at BIGINT);
CREATE TABLE IF NOT EXISTS group_photos (group_id TEXT, photo_id TEXT, date_added BIGINT, PRIMARY KEY (group_id, photo_id));
CREATE TABLE IF NOT EXISTS comments (id TEXT PRIMARY KEY, photo_id TEXT, author TEXT, author_name TEXT, real_name TEXT, date_created BIGINT, permalink TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS faves (photo_id TEXT, user_id TEXT, username TEXT, real_name TEXT, date_faved BIGINT, PRIMARY KEY (photo_id, user_id));
CREATE TABLE IF NOT EXISTS social_fetched (photo_id TEXT, kind TEXT, count INTEGER, fetched_at BIGINT, PRIMARY KEY (photo_id, kind));
CREATE TABLE IF NOT EXISTS photo_daily (photo_id TEXT, day TEXT, views INTEGER, comments INTEGER, favorites INTEGER, PRIMARY KEY (photo_id, day));
CREATE TABLE IF NOT EXISTS account_daily (day TEXT PRIMARY KEY, total INTEGER, photos INTEGER, photostream INTEGER, sets INTEGER, collections INTEGER, galleries INTEGER, fetched_at BIGINT);
CREATE TABLE IF NOT EXISTS stats_days (day TEXT PRIMARY KEY, status TEXT, photos_viewed INTEGER, fetched_at BIGINT);
CREATE TABLE IF NOT EXISTS wx_stations (station_id BIGINT PRIMARY KEY, name TEXT, lat DOUBLE, lon DOUBLE, elevation DOUBLE, timezone TEXT, fetched_at BIGINT);
CREATE TABLE IF NOT EXISTS wx_devices (device_id BIGINT PRIMARY KEY, station_id BIGINT, serial TEXT, device_type TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS wx_obs (device_id BIGINT, ts BIGINT, wind_lull DOUBLE, wind_avg DOUBLE, wind_gust DOUBLE, wind_dir DOUBLE, wind_interval DOUBLE, pressure DOUBLE, temp DOUBLE, humidity DOUBLE, illuminance DOUBLE, uv DOUBLE, solar DOUBLE, rain DOUBLE, precip_type INTEGER, strike_dist DOUBLE, strike_count INTEGER, battery DOUBLE, report_interval INTEGER, PRIMARY KEY (device_id, ts));
CREATE TABLE IF NOT EXISTS wx_events (device_id BIGINT, ts BIGINT, kind TEXT, value DOUBLE, energy DOUBLE, PRIMARY KEY (device_id, ts, kind));
CREATE TABLE IF NOT EXISTS aprs_positions (callsign TEXT, ts BIGINT, lat DOUBLE, lon DOUBLE, altitude DOUBLE, speed DOUBLE, course DOUBLE, symbol TEXT, comment TEXT, path TEXT, raw TEXT, PRIMARY KEY (callsign, ts));
CREATE TABLE IF NOT EXISTS aprs_igate (igate TEXT, callsign TEXT, ts BIGINT, lat DOUBLE, lon DOUBLE, direct BOOLEAN, format TEXT, path TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT, created_at BIGINT, updated_at BIGINT);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, username TEXT, csrf TEXT, created_at BIGINT, expires_at BIGINT, last_seen BIGINT, ip TEXT, user_agent TEXT);
CREATE TABLE IF NOT EXISTS settings (path TEXT PRIMARY KEY, value TEXT, updated_at BIGINT, updated_by TEXT);
CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, nonce BLOB, ciphertext BLOB, hint TEXT, updated_at BIGINT, updated_by TEXT);
CREATE SEQUENCE IF NOT EXISTS settings_log_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS applied_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS requested_jobs_seq START 1;
CREATE TABLE IF NOT EXISTS requested_jobs (kind TEXT, photo_id TEXT, "at" BIGINT, seq BIGINT DEFAULT nextval('requested_jobs_seq'), PRIMARY KEY (kind, photo_id));
CREATE TABLE IF NOT EXISTS applied (id BIGINT PRIMARY KEY DEFAULT nextval('applied_id_seq'), photo_id TEXT, "at" BIGINT, changes TEXT, undone_at BIGINT, result TEXT);
CREATE TABLE IF NOT EXISTS settings_log (id BIGINT PRIMARY KEY DEFAULT nextval('settings_log_id_seq'), "at" BIGINT, username TEXT, path TEXT, old TEXT, new TEXT);
"""


class Row(dict):
    """A result row readable by column name or by position, like sqlite3.Row."""
    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else super().__getitem__(key)

    def __iter__(self):
        return iter(self._values)


class Result:
    def __init__(self, cursor):
        self.cursor = cursor
        self.names = [c[0] for c in cursor.description] if cursor.description else []

    def fetchone(self):
        row = self.cursor.fetchone()
        return Row(self.names, row) if row is not None else None

    def fetchall(self):
        return [Row(self.names, row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


def _positional(sql: str, params: dict) -> tuple[str, list]:
    """:name placeholders -> ?, with the values in order."""
    names = []
    sql = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", lambda m: (names.append(m.group(1)) or "?"), sql)
    return sql, [params[n] for n in names]


_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _write_lock(path: str) -> threading.RLock:
    key = os.path.realpath(path)
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


class Connection:
    """One DuckDB connection. `with db:` is a transaction; nested ones join the outer one, and
    outside any transaction each statement commits by itself.

    DuckDB doesn't make a second writer wait for the first (SQLite did, up to its timeout): it fails
    at once with "Conflict on update". So everything that writes to a database file in this process
    takes one lock per file, for a statement or for a whole transaction; reads never wait."""
    def __init__(self, path: str, read_only: bool = False):
        self.raw = duckdb.connect(path, read_only=read_only)
        self._lock = _write_lock(path)
        self._depth = 0

    @staticmethod
    def _reads(sql: str) -> bool:
        words = sql.split(None, 1)
        return bool(words) and words[0].upper() in ("SELECT", "WITH", "DESCRIBE")

    def _run(self, sql, call):
        if self._reads(sql):
            return call()
        with self._lock:
            return call()

    def execute(self, sql, params=()):
        if isinstance(params, dict):
            sql, params = _positional(sql, params)
        return Result(self._run(sql, lambda: self.raw.execute(sql, params)))

    def executemany(self, sql, seq):
        seq = list(seq)
        if not seq:
            return Result(self.raw.execute("SELECT 1 WHERE FALSE"))
        if isinstance(seq[0], dict):
            marked, _ = _positional(sql, seq[0])
            seq = [_positional(sql, row)[1] for row in seq]
            sql = marked
        return Result(self._run(sql, lambda: self.raw.executemany(sql, seq)))

    def executescript(self, sql):
        for statement in sql.split(";"):
            if statement.strip():
                self._run(statement, lambda: self.raw.execute(statement))

    def __enter__(self):
        self._lock.acquire()
        try:
            if self._depth == 0:
                self.raw.begin()
        except BaseException:
            self._lock.release()
            raise
        self._depth += 1
        return self

    def __exit__(self, typ, value, tb):
        self._depth -= 1
        try:
            if self._depth == 0:
                self.raw.rollback() if typ else self.raw.commit()
        finally:
            self._lock.release()

    def commit(self):
        """Kept for callers written against sqlite3: statements outside `with db:` already committed."""

    def close(self):
        self.raw.close()


SCHEMA_VERSION = 11


def is_sqlite_file(path) -> bool:
    """A SQLite database, from before the move to DuckDB (any other file, or none: False)."""
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


class SchemaTooNew(RuntimeError):
    """The database was written by a newer version of the tool than this one."""


class Database:
    """The connection plus meta key/values. The other store classes build on this."""

    def __init__(self, path: str, migrate: bool = True):
        """A DuckDB connection can be handed between threads, one at a time.
        migrate=False skips the schema check (the web app runs it once at startup)."""
        self.path = path
        if is_sqlite_file(path):
            raise ValueError(f"{path} is a SQLite database from before the move to DuckDB, which this version can't "
                             "read. Converting one needs an older version (git history: flickr_organizer/store/migration.py).")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self.db = Connection(path)
        except duckdb.IOException as e:
            if "lock" not in str(e).lower():
                raise
            raise SystemExit(f"{path} is open in another process (the web app?). DuckDB lets one process use a "
                             "database at a time: stop that one first, or do this from the app's own pages.")
        if migrate:
            self.migrate()

    def close(self):
        self.db.close()

    def migrate(self):
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        version = int(self.get_meta("schema_version", 0))
        if version > SCHEMA_VERSION:
            raise SchemaTooNew(f"{self.path} is from a newer version of this tool (schema {version}); "
                               f"this one knows up to {SCHEMA_VERSION}. Update the tool.")
        if 0 < version < 5:                     # v5: comments_photo went; a DuckDB index that fell out of step with
            self.db.execute("DROP INDEX IF EXISTS comments_photo")   # its table makes deleting that row fatal
        if 0 < version < 6:                     # v6: queue_status went, the same way (re-queueing a photo replaces
            self.db.execute("DROP INDEX IF EXISTS queue_status")     # its row, and status changes on every job)
        if 0 < version < 8:                     # v8: sessions_expiry went, the same way (signing in deletes expired
            self.db.execute("DROP INDEX IF EXISTS sessions_expiry")  # sessions; the table only holds a few rows)
        if 0 < version < 9:                     # v9: and the last secondary indexes, before they do the same. These
            for name in ("memberships_photo", "group_photos_photo", "photo_daily_day"):   # tables scan in well
                self.db.execute(f"DROP INDEX IF EXISTS {name}")                          # under a millisecond
        if 0 < version < 10:                    # v10: the queue holds only photos in review (new, pending, applying):
            with self.db:                       # applied and rejected leave it; failed and "nothing to change" wait in it
                self.db.execute("DELETE FROM queue WHERE status IN ('applied', 'rejected')")
                self.db.execute("UPDATE queue SET status='pending' WHERE status IN ('none', 'failed')")
        self.db.executescript(SCHEMA)           # every statement is IF NOT EXISTS: nothing changes once it's there
        if 0 < version < 7:                     # v7: whether a photo's location is public on Flickr (filled by the next sync)
            self.db.execute("ALTER TABLE photos ADD COLUMN IF NOT EXISTS geo_public INTEGER")
        if 0 < version < 11:                    # v11: tags are proposed as one list: the "add these" proposals in review
            self._whole_tag_lists()             # become the photo's whole list (its tags plus those)
        if version != SCHEMA_VERSION:           # written only when it differs, so opening a connection never writes
            self.set_meta("schema_version", SCHEMA_VERSION)

    def _whole_tag_lists(self) -> None:
        """Each add_tags proposal of a photo waiting in review, as a set_tags of its tags plus the ones it added
        (gone if it had them all). Photos being applied keep theirs: add_tags still applies."""
        from ..changes import Change
        from ..util import norm_tag

        rows = self.db.execute("SELECT q.photo_id, q.changes, p.tags FROM queue q JOIN photos p ON p.id = q.photo_id "
                               "WHERE q.status IN ('new', 'pending') AND q.changes LIKE '%add_tags%'").fetchall()
        with self.db:
            for pid, changes, tags in rows:
                current = (tags or "").split()
                have = {norm_tag(t) for t in current}
                out = []
                for d in json.loads(changes or "[]"):
                    c = Change.from_dict(d)
                    if c.action == "add_tags":
                        added = [t for t in c.tags if norm_tag(t) not in have]
                        if not added:
                            continue
                        c = Change("set_tags", pid, current + added, old=" ".join(current), source=c.source)
                    out.append(c.to_dict())
                self.db.execute("UPDATE queue SET changes=? WHERE photo_id=?", (json.dumps(out), pid))

    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))
