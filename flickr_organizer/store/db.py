"""The SQLite connection, the schema, and numbered schema upgrades.

To change the schema: update SCHEMA (what a fresh database gets) *and* append a
function to MIGRATIONS that brings an existing database to the same shape. Each
upgrade runs once; the version reached is stored in meta.schema_version.
"""
from __future__ import annotations

import sqlite3
from typing import Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS photos (
    id TEXT PRIMARY KEY,
    title TEXT, description TEXT, tags TEXT,
    lat REAL, lon REAL, accuracy INTEGER,
    media TEXT, thumb_url TEXT, secret TEXT, server TEXT,
    date_taken TEXT, date_upload INTEGER, last_update INTEGER,
    ispublic INTEGER, date_taken_unknown INTEGER,
    views INTEGER, faves INTEGER, comments INTEGER, license TEXT, width INTEGER, height INTEGER,
    url_l TEXT, url_h TEXT, url_k TEXT
);
CREATE TABLE IF NOT EXISTS albums (
    id TEXT PRIMARY KEY, title TEXT, count INTEGER,
    date_update INTEGER, cover_url TEXT, description TEXT
);
CREATE TABLE IF NOT EXISTS memberships (
    album_id TEXT, photo_id TEXT, PRIMARY KEY (album_id, photo_id)
);
CREATE INDEX IF NOT EXISTS memberships_photo ON memberships(photo_id);
CREATE TABLE IF NOT EXISTS classifications (
    photo_id TEXT, fingerprint TEXT, result TEXT, created_at INTEGER,
    PRIMARY KEY (photo_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS cameras (
    photo_id TEXT PRIMARY KEY, camera TEXT, make TEXT, model TEXT, fetched_at INTEGER,
    lens TEXT, exif TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    photo_id TEXT, action TEXT, target TEXT, status TEXT, detail TEXT, at INTEGER,
    PRIMARY KEY (photo_id, action, target)
);
CREATE TABLE IF NOT EXISTS queue (
    photo_id TEXT PRIMARY KEY, status TEXT, changes TEXT, built_at INTEGER,
    reviewed_at INTEGER, result TEXT
);
CREATE INDEX IF NOT EXISTS queue_status ON queue(status);
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
""" + """
CREATE TABLE IF NOT EXISTS photo_daily (
    photo_id TEXT, day TEXT, views INTEGER, comments INTEGER, favorites INTEGER,
    PRIMARY KEY (photo_id, day)
);
CREATE INDEX IF NOT EXISTS photo_daily_day ON photo_daily(day);
CREATE TABLE IF NOT EXISTS account_daily (
    day TEXT PRIMARY KEY, total INTEGER, photos INTEGER, photostream INTEGER, sets INTEGER,
    collections INTEGER, galleries INTEGER, fetched_at INTEGER
);
CREATE TABLE IF NOT EXISTS stats_days (
    day TEXT PRIMARY KEY, status TEXT, photos_viewed INTEGER, fetched_at INTEGER
);
CREATE TABLE IF NOT EXISTS wx_stations (station_id BIGINT PRIMARY KEY, name TEXT, lat DOUBLE, lon DOUBLE, elevation DOUBLE, timezone TEXT, fetched_at BIGINT);
CREATE TABLE IF NOT EXISTS wx_devices (device_id BIGINT PRIMARY KEY, station_id BIGINT, serial TEXT, device_type TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS wx_obs (device_id BIGINT, ts BIGINT, wind_lull DOUBLE, wind_avg DOUBLE, wind_gust DOUBLE, wind_dir DOUBLE, wind_interval DOUBLE, pressure DOUBLE, temp DOUBLE, humidity DOUBLE, illuminance DOUBLE, uv DOUBLE, solar DOUBLE, rain DOUBLE, precip_type INTEGER, strike_dist DOUBLE, strike_count INTEGER, battery DOUBLE, report_interval INTEGER, PRIMARY KEY (device_id, ts));
CREATE TABLE IF NOT EXISTS wx_events (device_id BIGINT, ts BIGINT, kind TEXT, value DOUBLE, energy DOUBLE, PRIMARY KEY (device_id, ts, kind));
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY, password_hash TEXT, created_at INTEGER, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, username TEXT, csrf TEXT, created_at INTEGER, expires_at INTEGER,
    last_seen INTEGER, ip TEXT, user_agent TEXT
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS settings (
    path TEXT PRIMARY KEY, value TEXT, updated_at INTEGER, updated_by TEXT
);
CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY, nonce BLOB, ciphertext BLOB, hint TEXT, updated_at INTEGER, updated_by TEXT
);
CREATE TABLE IF NOT EXISTS settings_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER, username TEXT, path TEXT, old TEXT, new TEXT
);
"""


def _add_columns(db: sqlite3.Connection, table: str, columns: list[tuple[str, str]]) -> None:
    have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    for name, kind in columns:
        if name not in have:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


def _baseline(db: sqlite3.Connection) -> None:
    """1: every table as of the first versioned release. Databases created before
    versioning existed may lack columns added along the way, so add any missing."""
    db.executescript(SCHEMA)
    _add_columns(db, "photos", [("date_taken_unknown", "INTEGER"), ("views", "INTEGER"), ("faves", "INTEGER"),
                                ("comments", "INTEGER"), ("license", "TEXT"), ("width", "INTEGER"),
                                ("height", "INTEGER")])
    _add_columns(db, "cameras", [("lens", "TEXT"), ("exif", "TEXT")])


def _daily_stats(db: sqlite3.Connection) -> None:
    """2: zoom-size image URLs, album dates and covers (incremental sync, gallery), and
    per-day stats from Flickr: photo_daily (views/comments/faves per photo per day),
    account_daily (daily totals), stats_days (which days are fully fetched)."""
    _add_columns(db, "photos", [("url_l", "TEXT"), ("url_h", "TEXT"), ("url_k", "TEXT")])
    _add_columns(db, "albums", [("date_update", "INTEGER"), ("cover_url", "TEXT"), ("description", "TEXT")])
    db.executescript(SCHEMA)   # creates the new tables; CREATE ... IF NOT EXISTS leaves the rest


def _web_auth(db: sqlite3.Connection) -> None:
    """3: the web app's owner login (users) and its sessions. Only a hash of each session
    token is stored, so a copy of the database can't be used to log in."""
    db.executescript(SCHEMA)


def _admin_settings(db: sqlite3.Connection) -> None:
    """4: settings made on the admin page (settings: JSON per setting path, layered over
    config.yaml), API keys encrypted with a key kept outside the database (secrets), and
    the change history (settings_log)."""
    db.executescript(SCHEMA)


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_baseline, _daily_stats, _web_auth, _admin_settings]
SCHEMA_VERSION = len(MIGRATIONS)


class SchemaTooNew(RuntimeError):
    """The database was upgraded by a newer version of the tool than this one."""


class Database:
    """The connection plus meta key/values. The other store classes build on this."""

    def __init__(self, path: str, migrate: bool = True, any_thread: bool = False):
        """any_thread: allow this connection to be used from a thread other than the one
        that opened it. Only for a connection used by one caller at a time (the web app's
        one-per-request connection: FastAPI runs a request's dependencies and handler on
        different pool threads, one after another). migrate=False skips the upgrade check
        (the web app runs it once at startup)."""
        self.path = path
        # The review tool's background worker opens its own connection to the same file;
        # WAL lets the page keep reading while it writes, and the timeout waits out locks.
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=not any_thread)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        if migrate:
            self.migrate()

    def close(self) -> None:
        self.db.close()

    def migrate(self) -> None:
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        version = int(self.get_meta("schema_version", 0))
        if version > SCHEMA_VERSION:
            raise SchemaTooNew(f"{self.path} is from a newer version of this tool (schema {version}); "
                             f"this one knows up to {SCHEMA_VERSION}. Update the tool.")
        for number, step in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.db:
                step(self.db)
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(number),))

    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))
