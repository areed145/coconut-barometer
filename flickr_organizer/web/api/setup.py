"""First run, before there's a login: create one, or restore a backup (which brings its
logins). Both need the setup code from the server's log; both stop working once a login exists."""
from __future__ import annotations

from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..deps import client_ip, runtime, store
from ..security import MIN_PASSWORD, hash_password, same
from .admin import receive_restore, start_migration
from .auth import start_session

router = APIRouter(prefix="/api/setup", tags=["setup"])


class FirstUser(BaseModel):
    code: str = Field(max_length=100)
    username: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=MIN_PASSWORD, max_length=1000)


def _check(rt, request: Request, code: str) -> None:
    if not rt.needs_setup():
        raise HTTPException(409, "already set up; sign in instead")
    key = f"ip:{client_ip(request)}"
    if rt.login_throttle.retry_after(key):
        raise HTTPException(429, "too many wrong codes; try again later")
    if not (rt.setup_code and same(code.strip().upper(), rt.setup_code)):
        rt.login_throttle.failed(key)
        raise HTTPException(403, "wrong setup code (it's in the server's log)")


@router.get("")
def status(rt=Depends(runtime)):
    return {"needed": rt.needs_setup(), "sqlite_found": rt.migration.view()["source_found"]}


@router.post("/user")
def first_user(body: FirstUser, request: Request, response: Response, rt=Depends(runtime), db=Depends(store)):
    _check(rt, request, body.code)
    db.set_password_hash(body.username, hash_password(body.password))
    rt.setup_code = None
    return start_session(rt, db, request, response, body.username)


class Code(BaseModel):
    code: str = Field(max_length=100)


@router.post("/migrate")
def migrate(body: Code, request: Request, rt=Depends(runtime)):
    """Import the former SQLite library into this new database (needs the setup code). Poll
    /api/migration/status; when it says complete, sign in with the login you had."""
    _check(rt, request, body.code)
    return start_migration(rt)


@router.post("/restore")
async def restore(request: Request, rt=Depends(runtime)):
    """The backup as the raw body, the code in X-Setup-Code. The app restarts into it;
    sign in with a login from the backup."""
    _check(rt, request, unquote(request.headers.get("X-Setup-Code", "")))
    return await receive_restore(request, rt)
