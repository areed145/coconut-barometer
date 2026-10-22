"""Settings made on the admin page, encrypted API keys, and the change history.
Encryption itself is in coconut_barometer/settings.py; this only stores bytes."""
from __future__ import annotations

import json
import time

from .db import Database


class SettingsStore(Database):
    def admin_settings(self) -> dict[str, object]:
        """setting path ("vision.model") -> value."""
        return {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT path, value FROM settings")}

    def admin_setting_meta(self) -> dict[str, dict]:
        return {r[0]: {"updated_at": r[1], "updated_by": r[2]}
                for r in self.db.execute("SELECT path, updated_at, updated_by FROM settings")}

    def save_admin_settings(self, changes: dict[str, object], removed: list[str], username: str,
                            log: list[tuple[str, str, str]]) -> None:
        """changes: path -> value; removed: paths back to config.yaml/defaults; log: (path, old, new)."""
        now = int(time.time())
        with self.db:
            for path, value in changes.items():
                self.db.execute("INSERT INTO settings VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                                "value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                                (path, json.dumps(value), now, username))
            self.db.executemany("DELETE FROM settings WHERE path=?", [(p,) for p in removed])
            self.db.executemany('INSERT INTO settings_log ("at", username, path, old, new) VALUES (?, ?, ?, ?, ?)',
                                [(now, username, p, o, n) for p, o, n in log])

    def secret_rows(self) -> dict[str, dict]:
        return {r["name"]: dict(r) for r in self.db.execute("SELECT * FROM secrets")}

    def save_secret(self, name: str, nonce: bytes | None, ciphertext: bytes | None, hint: str, username: str) -> None:
        now = int(time.time())
        with self.db:
            if ciphertext is None:
                self.db.execute("DELETE FROM secrets WHERE name=?", (name,))
            else:
                self.db.execute("INSERT OR REPLACE INTO secrets VALUES (?, ?, ?, ?, ?, ?)",
                                (name, nonce, ciphertext, hint, now, username))
            self.db.execute('INSERT INTO settings_log ("at", username, path, old, new) VALUES (?, ?, ?, ?, ?)',
                            (now, username, name, "(secret)", "(cleared)" if ciphertext is None else f"(set, …{hint})"))

    def settings_log(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM settings_log ORDER BY id DESC LIMIT ?", (limit,))]
