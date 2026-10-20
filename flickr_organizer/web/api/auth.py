"""Login and logout. The only routes that work without a session."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..deps import client_ip, runtime, session, store
from ..security import COOKIE, new_token, token_hash, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=1000)


@router.post("/login")
def login(body: Login, request: Request, response: Response, db=Depends(store), rt=Depends(runtime)):
    ip = client_ip(request)
    keys = (f"ip:{ip}", f"user:{body.username.lower()}")
    throttle = rt.login_throttle
    wait = throttle.retry_after(*keys)
    if wait:
        raise HTTPException(429, "too many failed logins; try again later", headers={"Retry-After": str(wait)})
    ok, upgraded = verify_password(db.password_hash(body.username), body.password)
    if not ok:
        throttle.failed(*keys)
        raise HTTPException(401, "wrong username or password")
    throttle.succeeded(*keys)
    if upgraded:
        db.set_password_hash(body.username, upgraded)
    token, csrf = new_token(), new_token()
    days = rt.cfg["web"]["session_days"]
    db.create_session(token_hash(token), body.username, csrf, int(time.time()) + days * 86400, ip,
                      request.headers.get("user-agent", ""))
    response.set_cookie(COOKIE, token, max_age=days * 86400, httponly=True, samesite="strict",
                        secure=rt.cfg["web"]["secure_cookies"], path="/")
    return {"username": body.username, "csrf": csrf}


@router.get("/session")
def current(sess=Depends(session)):
    return {"username": sess["username"], "csrf": sess["csrf"], "expires_at": sess["expires_at"]}


@router.post("/logout")
def logout(request: Request, response: Response, sess=Depends(session), db=Depends(store), rt=Depends(runtime)):
    db.delete_session(token_hash(request.cookies[COOKIE]))
    response.delete_cookie(COOKIE, path="/", secure=rt.cfg["web"]["secure_cookies"], httponly=True, samesite="strict")
    return {"ok": True}
