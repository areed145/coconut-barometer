"""The FastAPI app: JSON API under /api (login required), the frontend under /,
the review worker and the sync scheduler in the background.

    coconut-barometer serve                # http://127.0.0.1:8000 (put Caddy in front on a server)

With no login yet, the page offers first-run setup: create one, or restore a backup.
"""
from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..store import SchemaTooNew
from .api import activity, admin, analytics, aprs, auth, gallery, maps, review, setup, sync, weather
from .deps import session
from .runtime import Runtime
from .security import security_headers

STATIC = Path(__file__).parent / "static"


class NoExit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def tracking_send(message):
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)
        try:
            await self.app(scope, receive, tracking_send)
        except (SystemExit, SchemaTooNew) as e:
            print(f"ERROR {scope.get('path')}: {e if isinstance(e, SchemaTooNew) else e.code}", file=sys.stderr, flush=True)
            if started:
                return
            body = json.dumps({"detail": "The server needs attention; see its log."}).encode()
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": body})


def create_app(cfg: dict, client, provider_factory=None, background: bool = True, scheduler: bool = True,
               process_kw: dict | None = None, cfg_path: str | None = None, client_factory=None) -> FastAPI:
    rt = Runtime(cfg, client, provider_factory, background=background, process_kw=process_kw,
                 cfg_path=cfg_path, client_factory=client_factory)
    web = cfg["web"]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if scheduler:
            rt.start_scheduler()
        yield
        rt.stop()

    app = FastAPI(title="Coconut Barometer", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url="/api/openapi.json" if web["api_docs"] else None)
    app.state.runtime = rt
    headers = security_headers(web["secure_cookies"])

    # Innermost (added first): a SystemExit raised while handling a request - the library's
    # "stop with a message", e.g. a database from a newer version or a config error during a
    # reload - would otherwise stop the whole server. It becomes a 503 for that request;
    # the message goes to the log only (it can include file paths).
    app.add_middleware(NoExit)

    @app.middleware("http")
    async def add_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in headers.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/"):
            # Always ask before using a saved copy (unchanged files come back as 304): the
            # scripts import each other, so one stale file after a deploy breaks the whole app.
            response.headers["Cache-Control"] = "no-cache"
        return response

    # Outermost: refuse Host headers that aren't ours (DNS rebinding, stray vhosts).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=web["allowed_hosts"])

    for module in (auth, setup, gallery, maps, review, analytics, sync, weather, aprs, activity, admin):
        app.include_router(module.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        """For the platform's health checks: no login, no details."""
        rt.store().db.execute("SELECT 1").fetchone()
        return {"ok": True}

    @app.get("/api/flickr/callback", name="flickr_callback", include_in_schema=False)
    def flickr_callback(oauth_token: str = "", oauth_verifier: str = ""):
        """Flickr sends the browser back here after you approve access. No session cookie
        arrives (SameSite=Strict on a cross-site return), so it's accepted only for a login
        this app started, in the last 10 minutes, once."""
        try:
            rt.flickr_login_finish(oauth_token, oauth_verifier)
        except PermissionError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        except Exception:
            return JSONResponse({"detail": "Flickr didn't accept the login; start again from Settings"}, status_code=502)
        return RedirectResponse("/#/settings", status_code=303)

    if web["api_docs"]:
        @app.get("/api/docs", include_in_schema=False, dependencies=[Depends(session)])
        def docs():
            return get_swagger_ui_html(openapi_url="/api/openapi.json", title="Coconut Barometer API")

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def no_such_api(rest: str):   # any method: an unknown API address is a 404, not a 405
        return JSONResponse({"detail": "not found"}, status_code=404)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/{rest:path}", include_in_schema=False)
    def frontend(rest: str):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
