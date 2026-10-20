"""The FastAPI app: JSON API under /api (login required), the frontend under /,
the review worker and the sync scheduler in the background.

    flickr-organizer user add <name>     # the owner login (needed before serving)
    flickr-organizer serve                # http://127.0.0.1:8000 (put Caddy in front on a server)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import admin, analytics, auth, gallery, review, sync
from .deps import session
from .runtime import Runtime
from .security import security_headers

STATIC = Path(__file__).parent / "static"


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

    app = FastAPI(title="Flickr Organizer", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url="/api/openapi.json" if web["api_docs"] else None)
    app.state.runtime = rt
    headers = security_headers(web["secure_cookies"])

    @app.middleware("http")
    async def add_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in headers.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # Outermost: refuse Host headers that aren't ours (DNS rebinding, stray vhosts).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=web["allowed_hosts"])

    for module in (auth, gallery, review, analytics, sync, admin):
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
            return get_swagger_ui_html(openapi_url="/api/openapi.json", title="Flickr Organizer API")

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def no_such_api(rest: str):   # any method: an unknown API address is a 404, not a 405
        return JSONResponse({"detail": "not found"}, status_code=404)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/{rest:path}", include_in_schema=False)
    def frontend(rest: str):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
