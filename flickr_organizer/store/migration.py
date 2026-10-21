"""The one-time import of the former SQLite database into DuckDB.

  import_sqlite(source, target)   offline: a new DuckDB file from a SQLite one (`migrate-sqlite`)
  Migration                       in the running app: copies into the live database, in the background,
                                  all in one transaction, with progress that survives a restart

Either way the SQLite file is only read. Every table's row count is checked against the source
before the copy is kept; any failure leaves the DuckDB database as it was."""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

SQLITE_HEADER = b"SQLite format 3\x00"
BATCH = 500
SKIP_META = {"schema_version"}      # the new database sets its own


def is_sqlite_file(path: str | os.PathLike) -> bool:
    try:
        with Path(path).open("rb") as f:
            return f.read(16) == SQLITE_HEADER
    except OSError:
        return False


def find_sqlite_source(target: str | os.PathLike, configured: str | None = None) -> Path | None:
    """The configured source if it is a SQLite file, else the one beside the DuckDB target
    (same name with .db / .sqlite / .sqlite3), else None."""
    target = Path(target)
    candidates = [Path(configured).expanduser()] if configured else []
    candidates += [target.with_suffix(s) for s in (".db", ".sqlite", ".sqlite3")]
    return next((c for c in candidates if is_sqlite_file(c)), None)


def _literal(v) -> str:
    """A SQLite value as a DuckDB SQL literal. Binding ~20 parameters a row is many times slower
    than literals for a library this size (2.3M values), and nothing but a quoted string, a number,
    NULL or hex ever reaches the statement."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v) if math.isfinite(v) else "NULL"
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"unhex('{bytes(v).hex()}')"
    return "'" + str(v).replace("\x00", "").replace("'", "''") + "'"


# ---- the copy ---------------------------------------------------------------------------
def copy_tables(source: str | os.PathLike, con, progress: Callable[[dict], None] = lambda u: None) -> dict[str, int]:
    """Copy every table both databases have into `con` (a store.db.Connection holding the schema),
    replacing what is there, and return the rows copied per table. The caller owns the transaction.
    Raises ValueError when the source is damaged or the counts don't match."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        ok = src.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise ValueError(f"the SQLite file failed its integrity check: {ok}")
        have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if "photos" not in have:
            raise ValueError("the SQLite file has no photos table; it isn't a Coconut Barometer library")
        known = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'")}
        tables = sorted(have & known)
        totals = {t: src.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
        rows_total, rows_done, counts = sum(totals.values()), 0, {}
        for n, table in enumerate(tables, 1):
            mine = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_name=?", (table,))}
            cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")') if r[1] in mine]
            if not cols:
                continue
            quoted = ",".join(f'"{c}"' for c in cols)
            keep = ""      # meta: everything but the keys the new database sets itself
            if table == "meta":
                keep = " WHERE key NOT IN (%s)" % ",".join(f"'{k}'" for k in SKIP_META)
            con.execute(f'DELETE FROM "{table}"{keep}')
            expected = src.execute(f'SELECT COUNT(*) FROM "{table}"{keep}').fetchone()[0]
            cursor, copied = src.execute(f'SELECT {quoted} FROM "{table}"{keep}'), 0
            while batch := cursor.fetchmany(BATCH):
                values = ",".join("(" + ",".join(_literal(v) for v in row) + ")" for row in batch)
                con.execute(f'INSERT OR REPLACE INTO "{table}" ({quoted}) VALUES {values}')
                copied += len(batch)
                rows_done += len(batch)
                progress({"table": table, "tables_done": n - 1, "tables_total": len(tables),
                          "rows_done": rows_done, "rows_total": rows_total})
            stored = con.execute(f'SELECT COUNT(*) FROM "{table}"{keep}').fetchone()[0]
            if copied != expected or stored != expected:
                raise ValueError(f"{table}: the SQLite file has {expected} rows, {copied} were read and DuckDB holds {stored}")
            counts[table] = copied
        if "settings_log" in counts:       # new log rows must get ids after the imported ones
            top = con.execute("SELECT COALESCE(MAX(id), 0) FROM settings_log").fetchone()[0]
            used = con.execute("SELECT COALESCE(MAX(last_value), 0) FROM duckdb_sequences() "
                               "WHERE sequence_name = 'settings_log_id_seq'").fetchone()[0]
            if top > used:
                con.execute("SELECT COUNT(nextval('settings_log_id_seq')) FROM range(?)", (int(top - used),))
        progress({"table": None, "tables_done": len(tables), "tables_total": len(tables),
                  "rows_done": rows_total, "rows_total": rows_total})
        return counts
    finally:
        src.close()


def import_sqlite(source: str | os.PathLike, target: str | os.PathLike, *, replace: bool = False,
                  progress: Callable[[dict], None] = lambda u: None) -> dict[str, int]:
    """A new DuckDB file at `target` from a SQLite file (written beside it and renamed into place when
    complete, so a failure leaves nothing behind)."""
    from .db import SCHEMA, SCHEMA_VERSION, Connection

    source, target = Path(source), Path(target)
    if not source.is_file():
        raise FileNotFoundError(source)
    if target.exists() and not replace:
        raise FileExistsError(target)
    tmp = target.with_name(target.name + ".importing")
    tmp.unlink(missing_ok=True)
    con = Connection(str(tmp))
    try:
        with con:
            con.executescript(SCHEMA)
            counts = copy_tables(source, con, progress)
            con.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        con.close()
        con = None
        os.replace(tmp, target)
        return counts
    except BaseException:
        if con:
            con.close()
        tmp.unlink(missing_ok=True)
        raise


# ---- status that survives a restart -----------------------------------------------------
def status_path(target: str | os.PathLike) -> Path:
    target = Path(target)
    return target.with_name(target.name + ".migration.json")


def write_status(target: str | os.PathLike, status: dict) -> None:
    path = status_path(target)
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(json.dumps(status, separators=(",", ":")))
    os.replace(partial, path)


def read_status(target: str | os.PathLike) -> dict:
    try:
        return json.loads(status_path(target).read_text())
    except (OSError, ValueError):
        return {"state": "not_started"}


# ---- in the running app -----------------------------------------------------------------
class MigrationError(RuntimeError):
    """The import can't start (the HTTP layer turns this into a 409)."""


class Migration:
    """Imports the SQLite file into the running app's database, once, in a background thread.

    States: not_started -> running -> complete | failed  (a failed one can be started again).
    The database must have no photos yet: a fresh server whose only content is the login made at
    setup. That is replaced by the SQLite file's users, sessions, settings and library, in one
    transaction, so a failure changes nothing and success needs no restart."""

    def __init__(self, db_path: str, source: Path | None, on_done: Callable[[], None] = lambda: None):
        self.db_path, self.source, self.on_done = db_path, source, on_done
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        status = read_status(db_path)
        if status.get("state") == "running":      # the server stopped mid-copy: nothing was committed
            status = {**status, "state": "failed", "error": "the server restarted before the import finished"}
            write_status(db_path, status)
        self.status = status

    def view(self) -> dict:
        """What the status endpoint shows: progress, never paths or secrets."""
        keys = ("state", "table", "tables_done", "tables_total", "rows_done", "rows_total",
                "started_at", "finished_at", "error")
        out = {k: self.status[k] for k in keys if k in self.status}
        out["source_found"] = bool(self.source and is_sqlite_file(self.source))
        if self.status.get("counts"):
            out["counts"] = self.status["counts"]
        return out

    def busy(self) -> bool:
        """An import is running: background jobs wait, so nothing else writes while the tables are being replaced."""
        return bool(self._thread and self._thread.is_alive())

    def _set(self, **kw) -> None:
        self.status = {**self.status, **kw}
        write_status(self.db_path, self.status)

    def start(self, open_store: Callable[[], object], idle: Callable[[], bool] = lambda: True) -> dict:
        """Begin the import. open_store() makes a Store on the app's database for the new thread;
        idle() says no background job (a sync, say) is writing to it right now."""
        with self._lock:
            if self.busy():
                raise MigrationError("an import is already running")
            if not idle():
                raise MigrationError("a background job is running; try again in a moment")
            if self.status.get("state") == "complete":
                raise MigrationError("the SQLite library has already been imported")
            if not self.source or not is_sqlite_file(self.source):
                raise MigrationError("there is no SQLite database to import")
            store = open_store()
            if store.db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]:
                store.close()
                raise MigrationError("this database already has photos; the import only fills a new one")
            self.status = {"state": "running", "started_at": int(time.time()), "tables_done": 0, "tables_total": None,
                           "rows_done": 0, "rows_total": None}
            write_status(self.db_path, self.status)
            self._thread = threading.Thread(target=self._run, args=(store,), name="sqlite-import", daemon=True)
            self._thread.start()
            return self.view()

    def _run(self, store) -> None:
        try:
            def progress(update):
                self.status = {**self.status, **update}
                write_status(self.db_path, self.status)

            with store.db:
                counts = copy_tables(self.source, store.db, progress)
            self._set(state="complete", finished_at=int(time.time()), counts=counts, table=None)
            store.set_meta("sqlite_imported_at", int(time.time()))
            try:
                self.on_done()
            except Exception as e:      # the import is kept; only refreshing the app's settings failed
                print(f"SQLite import finished, but refreshing settings failed: {e}", flush=True)
            print(f"Imported the SQLite library into DuckDB ({sum(counts.values())} rows).", flush=True)
        except Exception as e:
            self._set(state="failed", finished_at=int(time.time()), error=f"{type(e).__name__}: {e}"[:500])
            print(f"SQLite import failed: {type(e).__name__}: {e}", flush=True)
        finally:
            store.close()

    def wait(self, timeout: float = 600) -> None:
        if self._thread:
            self._thread.join(timeout)
