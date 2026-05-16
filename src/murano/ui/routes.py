"""Server-rendered page routes for the local UI.

Three pages: chat (/), browse (/browse), settings (/settings). All HTML is
produced server-side via Jinja; the chat page uses a small vanilla JS
client to consume the SSE stream from `/api/v1/ask`.
"""

from __future__ import annotations

from importlib import resources

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..config import Settings, get_api_key, load_settings
from ..index import db as chunks_db
from ..security import VaultPathError, safe_vault_path
from ..tree import retrieve as tree_retrieve

router = APIRouter()


def _templates_dir() -> str:
    return str(resources.files("murano.ui").joinpath("templates"))


templates = Jinja2Templates(directory=_templates_dir())


def get_settings() -> Settings:
    return load_settings()


@router.get("/", response_class=HTMLResponse)
def page_chat(request: Request, settings: Settings = Depends(get_settings)):
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "settings": settings,
            "page": "chat",
        },
    )


@router.get("/browse", response_class=HTMLResponse)
def page_browse(request: Request, settings: Settings = Depends(get_settings)):
    return templates.TemplateResponse(
        request,
        "browse.html",
        {
            "settings": settings,
            "page": "browse",
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def page_settings(request: Request, settings: Settings = Depends(get_settings)):
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
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": settings,
            "page": "settings",
            "api_key_present": bool(get_api_key()),
            "chunk_count": chunk_n,
            "file_count": file_n,
            "tree_status": tree_status,
        },
    )


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
