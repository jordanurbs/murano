"""Server-rendered page routes for the local UI.

Three pages: chat (/), browse (/browse), settings (/settings). All HTML is
produced server-side via Jinja; the chat page uses a small vanilla JS
client to consume the SSE stream from `/api/v1/ask`.
"""

from __future__ import annotations

import os
from importlib import resources

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..config import Settings, get_api_key, load_settings
from ..index import db as chunks_db
from ..security import VaultPathError, safe_vault_path
from ..tree import retrieve as tree_retrieve

router = APIRouter()

# Kept in sync with `murano.api.server.API_TOKEN_ENV`. We duplicate the
# constant rather than introduce a ui->api import cycle.
_API_TOKEN_ENV = "MURANO_API_TOKEN"


def _api_token_for_ui() -> str:
    """Return the API token to inject into the page meta tag.

    Reads MURANO_API_TOKEN at request time so a server restart with a new
    token is picked up without re-importing the app. Empty string -> no
    token configured -> the UI sends no X-Murano-Token header.
    """
    return (os.environ.get(_API_TOKEN_ENV, "") or "").strip()


def _templates_dir() -> str:
    return str(resources.files("murano.ui").joinpath("templates"))


templates = Jinja2Templates(directory=_templates_dir())


def get_settings() -> Settings:
    return load_settings()


def _base_context(settings: Settings, page: str) -> dict:
    """Shared template context: settings, current page tag, API token meta."""
    return {
        "settings": settings,
        "page": page,
        "api_token": _api_token_for_ui(),
    }


@router.get("/", response_class=HTMLResponse)
def page_chat(request: Request, settings: Settings = Depends(get_settings)):
    return templates.TemplateResponse(request, "chat.html", _base_context(settings, "chat"))


@router.get("/browse", response_class=HTMLResponse)
def page_browse(request: Request, settings: Settings = Depends(get_settings)):
    return templates.TemplateResponse(request, "browse.html", _base_context(settings, "browse"))


@router.get("/settings", response_class=HTMLResponse)
def page_settings(request: Request, settings: Settings = Depends(get_settings)):
    # Local import to avoid a circular dep (api/routes imports ui/routes
    # indirectly via the app factory). The helper is small.
    from ..api.routes import _effective_api_key_source

    chunk_n = 0
    file_n = 0
    if settings.chunks_db.exists():
        c = chunks_db.connect(settings.chunks_db)
        try:
            chunk_n = chunks_db.chunk_count(c)
            file_n = chunks_db.file_count(c)
        finally:
            c.close()
    tree_status = tree_retrieve.status(settings)
    ctx = _base_context(settings, "settings")
    ctx.update(
        {
            # Legacy: True iff the keychain has a Venice key. Kept for any
            # third-party UI that templates against this value.
            "api_key_present": bool(get_api_key()),
            # Audit fix: the keychain isn't the only key source. Show the
            # *effective* source so users on MURANO_VENICE_BASE_URL +
            # MURANO_API_KEY don't see a misleading "not set" prompt to run
            # `murano config set-key`.
            "api_key_source": _effective_api_key_source(settings),
            "chunk_count": chunk_n,
            "file_count": file_n,
            "tree_status": tree_status,
        }
    )
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.get("/file", response_class=HTMLResponse)
def page_file(path: str, request: Request, settings: Settings = Depends(get_settings)):
    """Render a single vault file (used by the browser side panel)."""
    try:
        candidate = safe_vault_path(settings.vault_root, path)
    except VaultPathError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    try:
        content = candidate.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return templates.TemplateResponse(
        request,
        "_file.html",
        {
            "path": path,
            "content": content,
        },
    )
