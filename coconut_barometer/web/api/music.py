"""Apple Music (apple_music.py): the Music page's report, and connecting your account.

Apple's sign-in only runs in its own script, in a popup that reports back to the page that opened it, and the site's
headers rightly stop both. So it gets one signed-in page of its own, /api/music/connect, with headers that let Apple's
script in there and nowhere else. It's under /api/ so the service worker leaves it alone (it keeps every other page
as the app's shell) and it's never cached. The page's only secrets are a developer token good for ten minutes and the
session's CSRF token."""
from __future__ import annotations

import html
import os
import tempfile
from urllib.parse import unquote

import duckdb
import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .. import assets
from ... import apple_music as M
from ... import settings as S
from ..deps import runtime, session, store
from .admin import _confirm_password

router = APIRouter(prefix="/api/music", tags=["music"])
MAX_IMPORT = 200 * 1024 ** 2                         # the small file; Apple's own is several times this

CONNECT_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self' https://js-cdn.music.apple.com; "
                               "style-src 'self' 'unsafe-inline'; img-src 'self' data: https://*.apple.com; "
                               "connect-src 'self' https://*.apple.com; frame-src https://*.apple.com; font-src 'self' https://*.apple.com; "
                               "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "Cross-Origin-Opener-Policy": "unsafe-none"}     # the sign-in popup reports back through window.opener
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Apple Music</title><link rel="stylesheet" href="/static/css/app.css">
<meta name="cb-csrf" content="{csrf}"><meta name="cb-developer-token" content="{token}">
<script src="/static/js/music_connect.js"></script>
<script src="https://js-cdn.music.apple.com/musickit/v3/musickit.js" async></script></head>
<body><main class="page"><div class="card"><h2>Connect Apple Music</h2>
<p>Apple asks you to sign in and allow access, then this site reads what you've played.</p>
<form id="connect"><input id="password" type="password" autocomplete="current-password" placeholder="Site password" required>
<button id="go" class="primary" disabled>Connect Apple Music</button></form>
<p id="note" class="why">Loading Apple's sign-in…</p><p><a href="/#/settings/connections">Back to Settings</a></p>
</div></main></body></html>"""


class Connect(BaseModel):
    token: str = Field(min_length=1, max_length=4096)
    password: str = Field(max_length=1000)


@router.get("/analytics", dependencies=[Depends(session)])
def analytics(days: int = Query(30, ge=1, le=M.ALL), tz: str = Query("UTC", max_length=64), rt=Depends(runtime), db=Depends(store)):
    return rt.music.report(db, days, tz)


@router.get("/state", dependencies=[Depends(session)])
def state(rt=Depends(runtime), db=Depends(store)):
    return rt.music.state(db)


@router.get("/connect", include_in_schema=False)
def connect_page(sess=Depends(session), rt=Depends(runtime)):
    try:
        token = M.developer_token(rt.cfg, M.BROWSER_TOKEN_SECONDS)
    except M.MusicError as e:
        raise HTTPException(409, str(e))
    body = PAGE.format(csrf=html.escape(sess["csrf"], quote=True), token=html.escape(token, quote=True))
    return HTMLResponse(assets.versioned(body), headers=CONNECT_HEADERS)


@router.post("/connect")
def connect(body: Connect, request: Request, sess=Depends(session), rt=Depends(runtime), db=Depends(store)):
    """Saved only once it has read the list: a token that can't, saved, would just fail an hour from now."""
    if "apple_music.user_token" in S.env_paths():
        raise HTTPException(409, "set by the environment on this server; change it there")
    _confirm_password(rt, db, request, sess["username"], body.password)
    try:
        M.fetch_recent(rt.cfg, user_token=body.token)
    except M.NotAuthorized as e:
        raise HTTPException(400, str(e))
    except (M.MusicError, requests.RequestException) as e:
        raise HTTPException(502, f"couldn't reach Apple Music: {e}")
    S.set_secret(db, "apple_music.user_token", body.token, sess["username"])
    db.set_meta("music_error", "")
    db.set_meta("music_last_poll", 0)                # the next minute's check reads it
    rt.reload()
    return rt.music.state(db)


@router.post("/import")
async def import_history(request: Request, sess=Depends(session), rt=Depends(runtime), db=Depends(store)):
    """The file from `coconut-barometer music-export` as the raw body, your password in X-Confirm-Password. Replaces
    what was imported before, so it can be run again with a newer export."""
    await run_in_threadpool(_confirm_password, rt, db, request, sess["username"],
                            unquote(request.headers.get("X-Confirm-Password", "")))
    fd, path = tempfile.mkstemp(suffix=".csv.gz", dir=os.path.dirname(os.path.abspath(rt.db_path)))
    try:
        with os.fdopen(fd, "wb") as f:
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_IMPORT:
                    raise HTTPException(413, "too big for the reduced file: run music-export on Apple's first")
                f.write(chunk)
        try:
            plays = await run_in_threadpool(db.music_import, path)
        except duckdb.Error:
            raise HTTPException(400, "that isn't a file from coconut-barometer music-export") from None
    finally:
        os.unlink(path)
    return {"plays": plays}
