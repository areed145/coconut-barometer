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
from fastapi.responses import FileResponse, JSONResponse
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

    if web["api_docs"]:
        @app.get("/api/docs", include_in_schema=False, dependencies=[Depends(session)])
        def docs():
            return get_swagger_ui_html(openapi_url="/api/openapi.json", title="Flickr Organizer API")

    @app.get("/api/{rest:path}", include_in_schema=False)
    def no_such_api(rest: str):
        return JSONResponse({"detail": "not found"}, status_code=404)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/{rest:path}", include_in_schema=False)
    def frontend(rest: str):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
