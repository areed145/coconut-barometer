"""The FastAPI app: JSON API under /api (login required), the frontend under /,
the review worker and the sync scheduler in the background.

    coconut-barometer serve                # http://127.0.0.1:8000 (put Caddy in front on a server)

With no login yet, the page offers first-run setup: create one, or restore a backup.
"""
from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..errors import caught, reporter
from ..store import SchemaTooNew
from .api import admin, analytics, aprs, auth, gallery, home, maps, push, review, setup, status, sync, visitors, weather
from .deps import session
from . import assets, share
from .runtime import Runtime
from .security import security_headers

STATIC = Path(__file__).parent / "static"
ROOT_TOUCH_ICON = re.compile(r"apple-touch-icon(-\d+x\d+)?(-precomposed)?\.png")
logger = logging.getLogger(__name__)


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
            logger.error("%s: %s", scope.get("path"), e if isinstance(e, SchemaTooNew) else e.code)
            if started:
                return
            body = json.dumps({"detail": "The server needs attention; see its log."}).encode()
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": body})


class Compress:
    """gzip for everything worth it (Starlette leaves images, fonts, archives and responses that are
    already encoded alone), except /api/auth/: those responses carry the CSRF token, and a compressed
    secret beside text an attacker can influence is what BREACH reads."""
    def __init__(self, app):
        self.app, self.gzip = app, GZipMiddleware(app, minimum_size=1000)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/auth/"):
            return await self.app(scope, receive, send)
        return await self.gzip(scope, receive, send)


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
    reporter.watch(cfg)
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
            # Data isn't cached, except a picture that says how (Home's radar, once per frame: weather/snapshot.py).
            if not (response.headers.get("content-type", "").startswith("image/") or response.status_code == 304) \
                    or "cache-control" not in response.headers:
                response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/v/") and response.status_code in (200, 304):
            # A versioned address never changes what it serves (assets.py): keep it for good.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif request.url.path.startswith("/static/"):
            # Unversioned (images, the font, the plain addresses): always ask before using a saved
            # copy; unchanged files come back as 304.
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.add_middleware(Compress)

    # Outermost: refuse Host headers that aren't ours (DNS rebinding, stray vhosts).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=web["allowed_hosts"])

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception):
        """A request that ends in a 500: reported (errors.py), then logged by the server as before."""
        reporter.report(exc, f"{request.method} {request.url.path}")
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)

    for module in (auth, setup, gallery, home, status, maps, review, analytics, sync, weather, aprs, push, admin, visitors):
        app.include_router(module.router)

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        """At the root, so it covers the whole site (a worker only controls the pages under its own address)."""
        return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/sw-precache.json", include_in_schema=False)
    def precache():
        """What sw.js keeps for offline use when it sees a new version: the code, the font, the icons."""
        return JSONResponse({"version": assets.version(), "files": [*assets.files(), "/static/fonts/dm-sans-latin.woff2",
                             "/static/img/icon-192.png", "/static/img/favicon-32.png", "/static/manifest.webmanifest"]},
                            headers={"Cache-Control": "no-cache"})

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
        except Exception as e:
            caught(e, "flickr login")
            return JSONResponse({"detail": "Flickr didn't accept the login; start again from Settings"}, status_code=502)
        return RedirectResponse("/#/settings/connections", status_code=303)

    if web["api_docs"]:
        @app.get("/api/docs", include_in_schema=False, dependencies=[Depends(session)])
        def docs():
            return get_swagger_ui_html(openapi_url="/api/openapi.json", title="Coconut Barometer API")

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def no_such_api(rest: str):   # any method: an unknown API address is a 404, not a 405
        return JSONResponse({"detail": "not found"}, status_code=404)

    static_files = StaticFiles(directory=STATIC)

    @app.get("/static/v/{build}/{path:path}", include_in_schema=False)
    async def versioned_static(build: str, path: str, request: Request):
        """The JS and CSS at the current version only (any other is a 404: see assets.py)."""
        if build != assets.version() or path.split("/", 1)[0] not in assets.VERSIONED:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return await static_files.get_response(path, request.scope)

    app.mount("/static", static_files, name="static")
    app.include_router(share.router)       # /p/<id>, /a/<id> (share links with previews), robots.txt, sitemap.xml

    # The icons browsers and iPhones look for at the root on their own, whatever the page links (Safari for a
    # notification's icon, iOS for a Home Screen one): apple-touch-icon[-180x180][-precomposed].png, favicon.ico.
    def root_icon(name: str) -> str | None:
        if name == "favicon.ico":
            return "favicon-32.png"
        return "apple-touch-icon.png" if ROOT_TOUCH_ICON.fullmatch(name) else None

    # GET and HEAD: icon fetchers and link checkers may ask with HEAD first, and a 405 there reads as no icon.
    @app.api_route("/{rest:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def frontend(rest: str, request: Request):
        if icon := root_icon(rest):
            return FileResponse(STATIC / "img" / icon, media_type="image/png",
                                headers={"Cache-Control": "public, max-age=86400"})
        if "." in rest.rsplit("/", 1)[-1]:     # a file that isn't here: a 404, not the page (which isn't an icon)
            return JSONResponse({"detail": "not found"}, status_code=404)
        return share.page(request, rt)

    return app
