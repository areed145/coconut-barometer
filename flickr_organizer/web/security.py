"""Login, sessions, CSRF and response headers for the web app.

- Passwords: argon2id (argon2-cffi). Failed logins are throttled per client IP and per
  username; verification always runs (against a dummy hash for unknown users), so timing
  doesn't reveal which usernames exist.
- Sessions: a random token in an HttpOnly, SameSite=Strict cookie (Secure unless
  web.secure_cookies is off for local http testing). Only its sha256 is stored.
- CSRF: every non-GET request must send the session's CSRF token in X-CSRF-Token.
- Headers: a strict Content-Security-Policy (scripts and styles from this app only,
  images also from Flickr's CDN), no framing, no sniffing, HSTS when cookies are secure.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

COOKIE = "fo_session"
CSRF_HEADER = "X-CSRF-Token"
MIN_PASSWORD = 12
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"use at least {MIN_PASSWORD} characters")
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> tuple[bool, str | None]:
    """(ok, new_hash): new_hash is set when the stored hash should be upgraded."""
    try:
        _hasher.verify(stored_hash or _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, None
    if stored_hash is None:
        return False, None
    return True, (_hasher.hash(password) if _hasher.check_needs_rehash(stored_hash) else None)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a, b)


class LoginThrottle:
    """At most `limit` failed logins per key (IP, or username) in `window` seconds."""

    def __init__(self, limit: int = 5, window: int = 15 * 60):
        self.limit, self.window = limit, window
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque:
        q = self._fails[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def retry_after(self, *keys: str) -> int:
        """Seconds until a login may be tried again (0 = now)."""
        now = time.time()
        with self._lock:
            waits = [int(q[0] + self.window - now) + 1 for k in keys
                     if len(q := self._prune(k, now)) >= self.limit]
        return max(waits, default=0)

    def failed(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for k in keys:
                self._prune(k, now).append(now)

    def succeeded(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)


CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data: https://*.staticflickr.com https://*.flickr.com; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def security_headers(secure: bool) -> dict[str, str]:
    h = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
         "Referrer-Policy": "same-origin", "Cross-Origin-Opener-Policy": "same-origin",
         "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()"}
    if secure:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return h
