"""FastAPI app factory + lifespan.

The app is wired here once and serves both the `/api/v1/*` REST routes and
the server-rendered UI pages. Background workers (apscheduler + vault
watcher) are bootstrapped in the lifespan so they share the server process.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import load_settings
from .routes import router as api_router
from .scheduler import (
    SchedulerHandles,
    start_background_workers,
    stop_background_workers,
)

# Audit-4 finding 2.5: optional shared-secret gate on mutating endpoints.
# Operators who deliberately bind to non-loopback can opt-in to require an
# X-Murano-Token header on POST endpoints that write state or burn tokens.
# Default behavior (no token configured) is unchanged: no auth, no header
# expected. The read endpoints (/health, /search, /chunks, /themes, /vault/*)
# stay open because their leakage surface is already minimized (loopback-only
# absolute paths, etc.).
API_TOKEN_ENV = "MURANO_API_TOKEN"
API_TOKEN_HEADER = "X-Murano-Token"
PROTECTED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _resolve_pkg_path(subpath: str) -> str:
    """Return an absolute filesystem path to a packaged resource under `murano.ui`."""
    pkg_root = resources.files("murano.ui")
    target = pkg_root.joinpath(subpath)
    return str(target)


def create_app(
    *,
    enable_schedule: bool = True,
    enable_watch: bool = True,
    api_token: str | None = None,
    bind_warning: str | None = None,
) -> FastAPI:
    """Build the FastAPI app. Pass enable_* False from tests to skip background workers."""
    settings = load_settings()

    handles: SchedulerHandles | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal handles
        if enable_schedule or enable_watch:
            handles = start_background_workers(
                settings,
                enable_schedule=enable_schedule,
                enable_watch=enable_watch,
            )
        try:
            yield
        finally:
            if handles is not None:
                stop_background_workers(handles)

    # Audit-4: the API token can come from --api-token (preferred) or from
    # the MURANO_API_TOKEN env var (so containers can inject it). An empty
    # value disables the gate.
    effective_token = (api_token or os.environ.get(API_TOKEN_ENV, "") or "").strip()

    description = (
        "Private, local-first personal knowledge base. By default, the "
        "only outbound network call this server makes is to api.venice.ai. "
        "Two narrowly-scoped exceptions exist by design: (1) `/api/v1/capture` "
        "and the RSS feed walker fetch user-supplied URLs restricted to "
        "public-internet hosts (see `murano.security.assert_public_http_url`); "
        "(2) the env var `MURANO_VENICE_BASE_URL` lets advanced users point at "
        "any OpenAI-compatible endpoint. The keychain Venice API key is only "
        "ever sent to api.venice.ai over HTTPS; custom endpoints use "
        "`MURANO_API_KEY`."
    )
    if bind_warning:
        description = "**" + bind_warning + "**\n\n" + description
    if effective_token:
        description += (
            "\n\nThis server requires the `X-Murano-Token` header on every "
            "POST/PUT/PATCH/DELETE request."
        )

    app = FastAPI(
        title="Murano",
        version=__version__,
        description=description,
        lifespan=lifespan,
    )

    if effective_token:
        @app.middleware("http")
        async def _require_token(request: Request, call_next):
            if request.method in PROTECTED_METHODS and request.url.path.startswith("/api/"):
                # Loopback callers (the bundled UI talking to itself) also need
                # the token. That's deliberate: it means if you set a token, a
                # malicious browser tab can no longer fire any mutating endpoint
                # via CSRF, even on loopback.
                sent = request.headers.get(API_TOKEN_HEADER, "")
                # Constant-time comparison to avoid timing-side-channel guessing.
                if not secrets.compare_digest(sent, effective_token):
                    # Return directly — raising HTTPException from middleware
                    # bypasses FastAPI's handler chain.
                    return JSONResponse(
                        status_code=401,
                        content={
                            "detail": (
                                f"Missing or invalid {API_TOKEN_HEADER} header."
                            )
                        },
                    )
            return await call_next(request)

    app.include_router(api_router)

    # UI: pages + static. Imported lazily so test fixtures can omit them.
    from ..ui.routes import router as ui_router

    app.include_router(ui_router)
    app.mount(
        "/static",
        StaticFiles(directory=_resolve_pkg_path("static")),
        name="static",
    )

    return app
