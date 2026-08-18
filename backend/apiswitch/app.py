from __future__ import annotations

from copy import copy
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from apiswitch import __version__
from apiswitch.api.admin import v2
from apiswitch.api.deps import require_admin_access
from apiswitch.config import settings
from apiswitch.db.bootstrap import init_database
from apiswitch.db.session import SessionLocal
from apiswitch.gateway.v2 import router as gateway_router
from apiswitch.harness_runtime import ensure_harness_token
from apiswitch.routing.engine import structured_error
from apiswitch.stream_compat import SSECompatibilityMiddleware


class DuplicateV1PrefixMiddleware:
    """Accept clients that append ``/v1/...`` to a Base URL ending in ``/v1``.

    Several desktop clients keep one OpenAI-style Base URL while switching the
    selected API format to Anthropic Messages.  Those clients can request
    ``/v1/v1/messages``.  Normalize exactly one duplicated version prefix and
    continue through the ordinary authenticated gateway route.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") in {"http", "websocket"}:
            path = str(scope.get("path") or "")
            if path == "/v1/v1" or path.startswith("/v1/v1/"):
                scope = dict(scope)
                scope["path"] = path[3:]
                raw_path = scope.get("raw_path")
                if isinstance(raw_path, bytes):
                    scope["raw_path"] = raw_path[3:]
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    if settings.plugin_mode:
        with SessionLocal() as db:
            ensure_harness_token(db)
    yield


def create_app() -> FastAPI:
    app=FastAPI(title=settings.app_name,version=__version__,lifespan=lifespan)
    app.add_middleware(DuplicateV1PrefixMiddleware)
    app.add_middleware(SSECompatibilityMiddleware)
    @app.exception_handler(Exception)
    async def unexpected(_, exc:Exception):
        return JSONResponse(status_code=500,content=structured_error("provider_unavailable","网关内部错误","internal"))
    @app.get("/health")
    async def health():return {"status":"ok","service":"apiswitch","version":__version__}
    app.include_router(gateway_router)
    app.include_router(_admin_router_for_mode(),dependencies=[Depends(require_admin_access)])
    dist=Path(settings.frontend_dist_dir).expanduser() if settings.frontend_dist_dir else Path(__file__).resolve().parents[2]/"frontend"/"dist"
    if (dist/"index.html").is_file(): app.mount("/ui",StaticFiles(directory=str(dist),html=True),name="frontend")
    return app


def _admin_router_for_mode():
    if not settings.plugin_mode:
        return v2.router
    omitted_prefixes = (
        "/api/admin/tokens",
        "/api/admin/agents",
        "/api/admin/settings/startup",
    )
    router = copy(v2.router)
    router.routes = [
        route
        for route in v2.router.routes
        if not str(getattr(route, "path", "")).startswith(omitted_prefixes)
    ]
    return router


app=create_app()
