"""FastAPI app factory + lifespan.

The app is wired here once and serves both the `/api/v1/*` REST routes and
the server-rendered UI pages. Background workers (apscheduler + vault
watcher) are bootstrapped in the lifespan so they share the server process.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import load_settings
from .routes import router as api_router
from .scheduler import (
    SchedulerHandles,
    start_background_workers,
    stop_background_workers,
)


def _resolve_pkg_path(subpath: str) -> str:
    """Return an absolute filesystem path to a packaged resource under `murano.ui`."""
    pkg_root = resources.files("murano.ui")
    target = pkg_root.joinpath(subpath)
    return str(target)


def create_app(
    *,
    enable_schedule: bool = True,
    enable_watch: bool = True,
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

    app = FastAPI(
        title="Murano",
        version=__version__,
        description=(
            "Private, local-first personal knowledge base. By default, the "
            "only outbound network call this server makes is to api.venice.ai. "
            "Two narrowly-scoped exceptions exist by design: (1) `/api/v1/capture` "
            "and the RSS feed walker fetch user-supplied URLs; (2) the env var "
            "`MURANO_VENICE_BASE_URL` lets advanced users point at any "
            "OpenAI-compatible endpoint. The keychain Venice API key is only "
            "ever sent to api.venice.ai; custom endpoints use `MURANO_API_KEY`."
        ),
        lifespan=lifespan,
    )

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
