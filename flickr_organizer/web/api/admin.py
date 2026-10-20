"""The admin page: settings (layered over config.yaml), API keys, account and sessions.

Changing an API key or the password needs the current password again, so a stolen
session can't swap keys; wrong attempts count towards the login throttle."""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ... import backup as B
from ... import settings as S
from ...config import read_raw
from ...settings_meta import SECTIONS
from ..deps import client_ip, runtime, session, store
from ..security import COOKIE, MIN_PASSWORD, hash_password, token_hash, verify_password

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(session)])


class SettingsChange(BaseModel):
    changes: dict[str, object] = {}
    reset: list[str] = []          # back to config.yaml / the default


class SecretChange(BaseModel):
    value: str | None = Field(None, max_length=500)    # empty or null clears it
    password: str = Field(max_length=1000)


class PasswordChange(BaseModel):
    current: str = Field(max_length=1000)
    new: str = Field(min_length=MIN_PASSWORD, max_length=1000)


def _confirm_password(rt, db, request: Request, username: str, password: str) -> None:
    keys = (f"ip:{client_ip(request)}", f"user:{username.lower()}")
    if rt.login_throttle.retry_after(*keys):
        raise HTTPException(429, "too many wrong passwords; try again later")
    ok, _ = verify_password(db.password_hash(username), password)
    if not ok:
        rt.login_throttle.failed(*keys)
        raise HTTPException(403, "wrong password")


def _payload(rt, db) -> dict:
    if not rt.cfg_path:
        raise HTTPException(409, "settings can't be edited in this session (no config file)")
    file_raw = read_raw(rt.cfg_path)
    _, sources, problems = S.layer(file_raw, db)
    return {"sections": [{"id": i, "title": t, "help": h} for i, t, h in SECTIONS],
            "settings": S.describe(rt.cfg, sources, db), "problems": problems,
            "albums": [a["title"] for a in db.albums()]}


@router.get("/settings")
def get_settings(rt=Depends(runtime), db=Depends(store)):
    return _payload(rt, db)


@router.put("/settings")
def put_settings(body: SettingsChange, sess=Depends(session), rt=Depends(runtime), db=Depends(store)):
    if not rt.cfg_path:
        raise HTTPException(409, "settings can't be edited in this session (no config file)")
    try:
        out = S.save(rt.cfg_path, read_raw(rt.cfg_path), db, body.changes, body.reset, sess["username"])
    except S.SettingsError as e:
        raise HTTPException(422, e.errors)
    rt.reload()
    return {**_payload(rt, db), "saved": out["changed"]}


@router.put("/secrets/{name}")
def put_secret(name: str, body: SecretChange, request: Request, sess=Depends(session), rt=Depends(runtime),
               db=Depends(store)):
    if name not in S.SECRETS:
        raise HTTPException(404, "not an API key setting")
    if name in S.env_paths():
        raise HTTPException(409, "set by the environment on this server; change it there")
    _confirm_password(rt, db, request, sess["username"], body.password)
    S.set_secret(db, name, body.value, sess["username"])
    rt.reload()
    return _payload(rt, db)


@router.get("/log")
def log(db=Depends(store)):
    return db.settings_log()


@router.post("/password")
def change_password(body: PasswordChange, request: Request, sess=Depends(session), rt=Depends(runtime),
                    db=Depends(store)):
    _confirm_password(rt, db, request, sess["username"], body.current)
    db.set_password_hash(sess["username"], hash_password(body.new))   # signs out every session
    return {"ok": True, "signed_out": True}


@router.get("/sessions")
def sessions(request: Request, sess=Depends(session), db=Depends(store)):
    mine = token_hash(request.cookies.get(COOKIE, ""))
    return [{"id": s["token_hash"][:16], "current": s["token_hash"] == mine, "created_at": s["created_at"],
             "last_seen": s["last_seen"], "expires_at": s["expires_at"], "ip": s["ip"], "user_agent": s["user_agent"]}
            for s in db.user_sessions(sess["username"])]


@router.delete("/sessions/{sid}")
def end_session(sid: str, sess=Depends(session), db=Depends(store)):
    match = [s for s in db.user_sessions(sess["username"]) if s["token_hash"].startswith(sid) and len(sid) >= 16]
    if len(match) != 1:
        raise HTTPException(404, "no such session")
    db.delete_session(match[0]["token_hash"])
    return {"ok": True}


class Confirm(BaseModel):
    password: str = Field(max_length=1000)


@router.get("/backups")
def backups(rt=Depends(runtime), db=Depends(store)):
    return {"backups": B.list_backups(rt.cfg), "every_hours": rt.cfg["backup"]["every_hours"],
            "last_backup": int(db.get_meta("last_backup") or 0) or None, "last_error": db.get_meta("last_backup_error")}


@router.post("/backups")
def backup_now(rt=Depends(runtime), db=Depends(store)):
    path = B.make_backup(rt.cfg)
    db.set_meta("last_backup", int(time.time()))
    return {"name": path.name}


@router.post("/backups/{name}/download")
def download_backup(name: str, body: Confirm, request: Request, sess=Depends(session), rt=Depends(runtime),
                    db=Depends(store)):
    """The whole database (comments, sessions, your password hash): password needed again."""
    _confirm_password(rt, db, request, sess["username"], body.password)
    try:
        path = B.backup_path(rt.cfg, name)
    except FileNotFoundError:
        raise HTTPException(404, "no such backup")
    return FileResponse(path, media_type="application/gzip", filename=name)


@router.get("/flickr")
def flickr_status(rt=Depends(runtime)):
    return rt.flickr_status()


@router.post("/flickr/connect")
def flickr_connect(request: Request, sess=Depends(session), rt=Depends(runtime)):
    callback = str(request.url_for("flickr_callback"))
    try:
        return {"url": rt.flickr_login_start(callback, sess["username"])}
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(502, f"Flickr didn't start the login: {e}")


@router.post("/restore")
async def upload_restore(request: Request, sess=Depends(session), rt=Depends(runtime), db=Depends(store)):
    """Upload a backup (.db.gz or .db) as the raw request body, your password in
    X-Confirm-Password. Checked, then restored by restarting the app."""
    from urllib.parse import unquote

    _confirm_password(rt, db, request, sess["username"], unquote(request.headers.get("X-Confirm-Password", "")))
    return await receive_restore(request, rt)


MAX_UPLOAD = 2 * 1024 ** 3   # 2 GB


async def receive_restore(request: Request, rt) -> dict:
    """The request body (a backup) into the data directory, checked and staged, then a
    restart restores it. X-Encryption-Key (optional, URI-encoded): the key its API keys are
    encrypted with (the contents of secret.key), when it comes from another machine."""
    import tempfile
    from pathlib import Path
    from urllib.parse import unquote

    from ...config import data_dir

    folder = data_dir() or Path(rt.cfg["database"]).parent
    fd, tmp = tempfile.mkstemp(dir=folder, prefix="upload-", suffix=".part")
    size = 0
    try:
        with open(fd, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "that file is too big for a backup")
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "no file received")
        try:
            out = B.stage_restore(rt.cfg, tmp, unquote(request.headers.get("X-Encryption-Key", "")))
        except ValueError as e:
            raise HTTPException(422, str(e))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    rt.restart_soon()
    return {**out, "size": size, "restarting": True}
