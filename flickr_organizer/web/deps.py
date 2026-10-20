"""Request dependencies: the runtime, this thread's store, and the logged-in session."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from ..store import Store
from .runtime import Runtime
from .security import COOKIE, CSRF_HEADER, SAFE_METHODS, same, token_hash


def runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def store(rt: Runtime = Depends(runtime)):
    """One connection per request, closed when it ends. FastAPI runs a request's
    dependencies and its handler on different pool threads (one after another, never at
    once), so the connection may move between threads but is never shared by two requests."""
    db = Store(rt.db_path, migrate=False, any_thread=True)
    try:
        yield db
    finally:
        db.close()


def session(request: Request, db: Store = Depends(store)) -> dict:
    """The logged-in session, or 401. Non-GET requests also need the CSRF token (403)."""
    token = request.cookies.get(COOKIE)
    sess = db.session(token_hash(token)) if token else None
    if not sess:
        raise HTTPException(401, "not logged in")
    if request.method not in SAFE_METHODS and not same(request.headers.get(CSRF_HEADER), sess["csrf"]):
        raise HTTPException(403, "missing or wrong CSRF token")
    return sess


def client_ip(request: Request) -> str:
    """uvicorn already resolves X-Forwarded-For for web.trusted_proxies."""
    return request.client.host if request.client else "?"
