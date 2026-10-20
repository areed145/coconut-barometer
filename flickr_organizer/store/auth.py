"""The web app's users and sessions. Session tokens are stored hashed (sha256)."""
from __future__ import annotations

import time

from .db import Database


class AuthStore(Database):
    def set_password_hash(self, username: str, password_hash: str) -> None:
        now = int(time.time())
        with self.db:
            self.db.execute("INSERT INTO users VALUES (?, ?, ?, ?) ON CONFLICT(username) DO UPDATE SET "
                            "password_hash=excluded.password_hash, updated_at=excluded.updated_at",
                            (username, password_hash, now, now))
            # A new password ends every existing session for that user.
            self.db.execute("DELETE FROM sessions WHERE username=?", (username,))

    def password_hash(self, username: str) -> str | None:
        r = self.db.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()
        return r[0] if r else None

    def usernames(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT username FROM users ORDER BY username")]

    def delete_user(self, username: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM users WHERE username=?", (username,))
            self.db.execute("DELETE FROM sessions WHERE username=?", (username,))

    def create_session(self, token_hash: str, username: str, csrf: str, expires_at: int, ip: str, user_agent: str):
        now = int(time.time())
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            self.db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (token_hash, username, csrf, now, expires_at, now, ip, user_agent[:300]))

    def session(self, token_hash: str) -> dict | None:
        r = self.db.execute("SELECT * FROM sessions WHERE token_hash=? AND expires_at >= ?",
                            (token_hash, int(time.time()))).fetchone()
        return dict(r) if r else None

    def user_sessions(self, username: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM sessions WHERE username=? AND expires_at >= ? ORDER BY last_seen DESC",
            (username, int(time.time())))]

    def touch_session(self, token_hash: str) -> None:
        with self.db:
            self.db.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (int(time.time()), token_hash))

    def delete_session(self, token_hash: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
