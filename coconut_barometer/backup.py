"""Backups of the database, safe while the app is running.

DuckDB's COPY FROM DATABASE writes a consistent snapshot while the app is running (a plain
file copy of a live database can come back corrupted, which is also why Render's disk
snapshots aren't enough on their own). Each backup is gzip-compressed; the newest `backup.keep`
are kept. API keys stay encrypted inside, so a backup doesn't reveal them, and it can't be
read back without the encryption key (keep that separately).
"""
from __future__ import annotations

import gzip
import os
import shutil
import tempfile
import time
from pathlib import Path

from .store.db import is_sqlite_file

PREFIX, SUFFIX = "coconut_barometer_", ".db.gz"
OLD_PREFIX = "flickr_organizer-"            # made before the rename: still listed, downloadable and pruned


def backup_dir(cfg: dict) -> Path:
    d = cfg["backup"]["dir"]
    return Path(os.path.expanduser(d)) if d else Path(cfg["database"]).parent / "backups"


def list_backups(cfg: dict) -> list[dict]:
    d = backup_dir(cfg)
    files = [f for p in (PREFIX, OLD_PREFIX) for f in d.glob(f"{p}*{SUFFIX}")] if d.exists() else []
    files.sort(key=lambda f: f.name.removeprefix(PREFIX).removeprefix(OLD_PREFIX), reverse=True)   # newest first, by the time in the name
    return [{"name": f.name, "size": f.stat().st_size, "created_at": int(f.stat().st_mtime)} for f in files]


def make_backup(cfg: dict) -> Path:
    d = backup_dir(cfg)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = d / f"{PREFIX}{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}{SUFFIX}"
    with tempfile.TemporaryDirectory(dir=d) as tmp:
        plain = Path(tmp) / "snapshot.db"
        import duckdb
        src = duckdb.connect(cfg["database"])
        try:
            escaped = str(plain).replace("'", "''")
            database = src.execute("SELECT current_database()").fetchone()[0].replace('"', '""')
            src.execute(f"ATTACH '{escaped}' AS snapshot")
            src.execute(f'COPY FROM DATABASE "{database}" TO snapshot')
            src.execute("DETACH snapshot")
        finally:
            src.close()
        partial = Path(tmp) / "snapshot.db.gz"
        with open(plain, "rb") as fin, gzip.open(partial, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
        os.chmod(partial, 0o600)
        os.replace(partial, target)
    for old in list_backups(cfg)[cfg["backup"]["keep"]:]:
        (d / old["name"]).unlink(missing_ok=True)
    return target


def backup_path(cfg: dict, name: str) -> Path:
    """A backup by name, refusing anything that isn't one of ours (no path tricks)."""
    if not (name.startswith((PREFIX, OLD_PREFIX)) and name.endswith(SUFFIX)) or "/" in name or "\\" in name or ".." in name:
        raise FileNotFoundError(name)
    p = backup_dir(cfg) / name
    if not p.is_file():
        raise FileNotFoundError(name)
    return p


def _gzipped(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


OLD_SQLITE = "a backup from before the move to DuckDB (SQLite), which this version can't restore"


def check_backup_file(path: str, key: bytes | None = None) -> dict:
    """Raise ValueError unless it's a usable backup (.db.gz or .db): readable, intact, ours.
    Returns {"keys": n API keys inside, "readable": whether `key` (or this machine's) opens them}."""
    from . import settings as S

    p = Path(path)
    with tempfile.TemporaryDirectory(dir=p.parent) as tmp:
        plain = Path(tmp) / "check.db"
        try:
            with (gzip.open if _gzipped(p) else open)(p, "rb") as fin, open(plain, "wb") as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)
            if is_sqlite_file(plain):
                raise ValueError(OLD_SQLITE)
            import duckdb
            con = duckdb.connect(str(plain), read_only=True)
            try:
                ok = con.execute("SELECT 1").fetchone()[0] == 1
                tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
                rows = con.execute("SELECT name, nonce, ciphertext FROM secrets").fetchall() if "secrets" in tables else []
            finally:
                con.close()
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"not a database backup ({e})")
    if ok is not True:
        raise ValueError(f"the database is damaged ({ok})")
    if "photos" not in tables:
        raise ValueError("not a Coconut Barometer library database (no photos table)")
    readable = all(S.decrypt(name, nonce, ct, key) is not None for name, nonce, ct in rows)
    return {"keys": len(rows), "readable": readable}


def stage_restore(cfg: dict, upload: str, key_text: str | None = None) -> dict:
    """Put a checked backup where the next start restores it (data dir / restore.db.gz),
    with the encryption key it came with (restore.key), if given. A key that doesn't open
    the backup's API keys is refused (ValueError), so a typo can't lock them away."""
    from . import settings as S
    from .config import data_dir

    try:
        key = S._key_from_text(key_text) if key_text and key_text.strip() else None
    except SystemExit as e:
        raise ValueError(str(e.code))
    info = check_backup_file(upload, key)
    if key and info["keys"] and not info["readable"]:
        raise ValueError("that encryption key doesn't open the API keys in this backup")
    folder = data_dir() or Path(cfg["database"]).parent
    target = folder / ("restore.db.gz" if _gzipped(Path(upload)) else "restore.db")
    for old in (folder / "restore.db.gz", folder / "restore.db", folder / "restore.key"):
        old.unlink(missing_ok=True)
    if key:
        fd = os.open(folder / "restore.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key_text.strip())
    os.replace(upload, target)
    os.chmod(target, 0o600)
    return {"staged": target.name, **info, "key_given": bool(key)}


def restore(cfg: dict, source: str) -> Path:
    """Replace the database with a backup (.db.gz or .db). Stop the app first, and close every connection to the database. The current
    database is kept beside it as <name>.before-restore-<time>."""
    src = Path(source)
    with tempfile.TemporaryDirectory(dir=Path(cfg["database"]).parent) as tmp:
        plain = Path(tmp) / "restore.db"
        opener = gzip.open if src.suffix == ".gz" else open
        with opener(src, "rb") as fin, open(plain, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
        if is_sqlite_file(plain):
            raise SystemExit(f"{source} is {OLD_SQLITE}")
        import duckdb
        check = duckdb.connect(str(plain), read_only=True)
        try:
            check.execute("SELECT 1").fetchone()
            tables = {r[0] for r in check.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
        finally:
            check.close()
        if "photos" not in tables:
            raise SystemExit(f"{source} isn't a usable backup (no photos table)")
        db = Path(cfg["database"])
        kept = db.with_name(f"{db.name}.before-restore-{time.strftime('%Y%m%d-%H%M%S')}")
        if db.exists():
            try:           # fold the write-ahead log in (a stopped process leaves one), so the kept copy is complete
                import duckdb

                con = duckdb.connect(str(db))
                con.execute("CHECKPOINT")
                con.close()
            except Exception:
                pass
            shutil.copy2(db, kept)
        wal = db.with_name(db.name + ".wal")
        wal.unlink(missing_ok=True)      # it belongs to the database being replaced; replaying it onto the backup would corrupt it
        os.replace(plain, db)
    return kept
