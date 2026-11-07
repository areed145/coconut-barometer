"""Apple Music (apple_music.py): the Music page's report, and connecting your account.

Apple's sign-in only runs in its own script, in a popup that reports back to the page that opened it, and the site's
headers rightly stop both. So it gets one signed-in page of its own, /api/music/connect, with headers that let Apple's
script in there and nowhere else. It's under /api/ so the service worker leaves it alone (it keeps every other page
as the app's shell) and it's never cached. The page's only secrets are a developer token good for ten minutes and the
session's CSRF token."""
from __future__ import annotations

import hashlib
import html
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

import duckdb
import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .. import assets
from ... import apple_music as M
from ... import local
from ... import settings as S
from ..deps import runtime, session, store
from .admin import _confirm_password

router = APIRouter(prefix="/api/music", tags=["music"])
ART_SIZES = (64, 160, 320, 640)
ART_MAX_BYTES = 5 * 1024 ** 2
ART_SECONDS = 30 * 86400
MAX_IMPORT = 200 * 1024 ** 2                         # the small file; Apple's own is several times this

CONNECT_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self' https://js-cdn.music.apple.com; "
                               "style-src 'self' 'unsafe-inline'; img-src 'self' data: https://*.apple.com; "
                               "connect-src 'self' https://*.apple.com; frame-src https://*.apple.com; font-src 'self' https://*.apple.com; "
                               "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "Cross-Origin-Opener-Policy": "unsafe-none",     # the sign-in popup reports back through window.opener
    "Referrer-Policy": "strict-origin"}              # Apple's popup reads our address from the referrer: without it, Unauthorized
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
<p id="note" class="why">Loading Apple's sign-in…</p><p><a href="/#/settings/music">Back to Settings</a></p>
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


@router.get("/weather", dependencies=[Depends(session)])
def weather(days: int = Query(M.ALL, ge=1, le=M.ALL), tz: str = Query("UTC", max_length=64), db=Depends(store)):
    return M.rain_report(db, days, tz)


@router.get("/trip/{trip_id}", dependencies=[Depends(session)])
def trip(trip_id: str, tz: str = Query("UTC", max_length=64), db=Depends(store)):
    """A trip's soundtrack: signed in only, though the trip itself may be public."""
    found = db.trip_list(trip_id=trip_id)
    if not found:
        raise HTTPException(404, "no such trip")
    return M.trip_soundtrack(db, found[0]["first"], found[0]["last"], tz)


def _cover_url(template: str, size: int) -> str:
    url = template.replace("{w}", str(size)).replace("{h}", str(size)).replace("{c}", "bb").replace("{f}", "jpg")
    u = urlparse(url)
    if u.scheme != "https" or not (u.hostname or "").endswith(".mzstatic.com"):     # never fetch anywhere else
        raise HTTPException(404, "no cover")
    return url


@router.get("/art/{track_id}", dependencies=[Depends(session)])
def art(track_id: str, s: int = Query(160), rt=Depends(runtime), db=Depends(store)):
    """A cover, from this server's own copy: Apple's image host isn't in the site's CSP, and this way the browser
    never talks to it. Fetched the first time it's shown, then kept on the disk."""
    if s not in ART_SIZES:
        raise HTTPException(400, "unknown size")
    headers = {"Cache-Control": f"private, max-age={ART_SECONDS}"}
    folder = Path(rt.db_path).resolve().parent / "music_art"
    name = hashlib.sha256(track_id.encode()).hexdigest()[:32]
    path = folder / f"{name}-{s}.jpg"
    if path.exists():
        return FileResponse(path, media_type="image/jpeg", headers=headers)
    template = db.music_artwork(track_id)
    if not template:
        raise HTTPException(404, "no cover")
    try:
        r = requests.get(_cover_url(template, s), timeout=20, allow_redirects=False)
    except requests.RequestException:
        raise HTTPException(502, "Apple's image host didn't answer") from None
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/") or len(r.content) > ART_MAX_BYTES:
        raise HTTPException(502, "Apple's image host didn't send a cover")
    folder.mkdir(exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.part")
    temporary.write_bytes(r.content)
    temporary.replace(path)
    return FileResponse(path, media_type=r.headers["content-type"].split(";")[0], headers=headers)


@router.post("/sync", dependencies=[Depends(session)])
def sync(rt=Depends(runtime)):
    """A read now, as a job (Settings → Music). Not on a local copy: each read builds on the one before."""
    if local.on():
        raise HTTPException(409, "reads are held in local mode")
    return rt.review.music_sync()


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
