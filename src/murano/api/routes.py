"""`/api/v1/*` route handlers.

The streaming `/api/v1/ask` endpoint emits Server-Sent Events that mirror
the internal `AnswerEvent` shape from `chat.answer`:

    event: retrieval
    data: {"hits": [...], "summaries": [...], "embed_model": "...", ...}

    event: delta
    data: {"text": "..."}

    event: done
    data: {"text": "...", "finish_reason": "stop", "cited": [...]}

    event: error
    data: {"text": "..."}

This is a clean mapping for the browser-side fetch/ReadableStream consumer
and identical to what an external agent would receive.
"""

from __future__ import annotations

import json
import platform
import subprocess
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import __version__
from ..capture.web import CaptureError, capture_url
from ..chat.answer import StreamConfig, extract_citation_keys, stream_answer
from ..chat.retriever import Retriever
from ..config import Settings, get_api_key, load_settings
from ..index import db as chunks_db
from ..index.indexer import index_vault
from ..tree import retrieve as tree_retrieve
from ..venice import VeniceAuthError, VeniceConnectionError, resolve_models
from . import schemas

router = APIRouter(prefix="/api/v1")


def get_settings() -> Settings:
    """FastAPI dependency: load settings on each request (cheap, no I/O cost)."""
    return load_settings()


@router.get("/health", response_model=schemas.HealthResponse)
def health(settings: Settings = Depends(get_settings)):
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

    return schemas.HealthResponse(
        status="ok",
        version=__version__,
        vault_root=str(settings.vault_root),
        data_root=str(settings.data_root),
        chunks_db_exists=settings.chunks_db.exists(),
        summary_tree_exists=tree_status.exists,
        chunk_count=chunk_n,
        file_count=file_n,
        tree_node_count=tree_status.node_count,
        tree_stale=tree_status.is_stale,
        tree_stale_reason=tree_status.stale_reason,
        api_key_present=bool(get_api_key()),
        chat_model=settings.chat_model,
        embed_model=settings.embed_model,
    )


@router.post("/search", response_model=schemas.SearchResponse)
def search(body: schemas.SearchRequest, settings: Settings = Depends(get_settings)):
    if not settings.chunks_db.exists():
        raise HTTPException(
            status_code=409,
            detail=f"No index found at {settings.chunks_db}. Run `murano index` first.",
        )
    try:
        with Retriever.open(settings) as r:
            result = r.retrieve(body.query, k=body.k, include_summaries=False)
    except VeniceAuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except VeniceConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    hits = [
        schemas.SearchHitResponse(
            rank=i + 1,
            chunk_id=h.chunk_id,
            file_path=h.file_path,
            heading_path=h.heading_path,
            citation=f"[[{h.citation_key}]]",
            distance=h.distance,
            token_count=h.token_count,
            content=h.content,
        )
        for i, h in enumerate(result.hits)
    ]
    return schemas.SearchResponse(
        query=body.query,
        embed_model=result.embed_model,
        elapsed_ms=result.elapsed_ms,
        hits=hits,
    )


@router.post("/capture", response_model=schemas.CapturedResponse)
def capture(body: schemas.CaptureRequest, settings: Settings = Depends(get_settings)):
    if not settings.vault_root.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Vault does not exist at {settings.vault_root}. Run `murano init` first.",
        )
    try:
        page = capture_url(settings, body.url, extra_tags=body.tags or None)
    except CaptureError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    chunks_indexed = 0
    try:
        report = index_vault(settings, subpath=Path(page.relpath))
        chunks_indexed = report.chunks_inserted
    except VeniceAuthError:
        chunks_indexed = -1  # captured but not indexed; sentinel for clients
    except VeniceConnectionError:
        chunks_indexed = -1

    return schemas.CapturedResponse(
        url=page.url,
        title=page.title,
        relpath=page.relpath,
        absolute_path=str(page.absolute_path),
        word_count=page.word_count,
        byte_count=page.byte_count,
        site_name=page.site_name,
        published_date=page.published_date,
        chunks_indexed=chunks_indexed,
    )


@router.get("/chunks/{chunk_id:path}", response_model=schemas.ChunkResponse)
def get_chunk(chunk_id: str, settings: Settings = Depends(get_settings)):
    rec = tree_retrieve.get_chunk(settings, chunk_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Chunk not found: {chunk_id}")
    return schemas.ChunkResponse(
        chunk_id=rec.chunk_id,
        file_path=rec.file_path,
        ord=rec.ord,
        heading_path=rec.heading_path,
        content=rec.content,
        token_count=rec.token_count,
        byte_offset=rec.byte_offset,
    )


@router.get("/themes", response_model=schemas.ThemesResponse)
def list_themes(level: int = 1, settings: Settings = Depends(get_settings)):
    if level < 1 or level > 6:
        raise HTTPException(status_code=400, detail="level must be 1..6")
    nodes = tree_retrieve.list_themes(settings, level=level)
    return schemas.ThemesResponse(
        level=level,
        themes=[
            schemas.ThemeResponse(
                id=n.id,
                level=n.level,
                title=n.title,
                summary=n.summary,
                member_count=n.member_count,
                parent_id=n.parent_id,
            )
            for n in nodes
        ],
    )


@router.post("/open")
def open_file(body: schemas.OpenRequest, settings: Settings = Depends(get_settings)):
    """Open a vault file in the OS default editor. Vault-relative paths only.

    This intentionally rejects absolute paths and any path that resolves
    outside the vault root — we don't want to be a generic file opener.
    """
    candidate = (settings.vault_root / body.path).resolve()
    vault_root = settings.vault_root.resolve()
    if not str(candidate).startswith(str(vault_root)):
        raise HTTPException(status_code=400, detail="Path is outside the vault.")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {body.path}")

    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", str(candidate)], check=True)
        elif system == "Windows":
            import os

            os.startfile(str(candidate))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(candidate)], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to open {body.path}: {e}"
        ) from e
    return {"status": "opened", "path": str(candidate)}


# --- SSE streaming --- ---


def _sse(event: str, data: dict | str) -> bytes:
    """Format one SSE message. `data` is serialized as JSON unless already a string."""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode()


def _ask_event_stream(settings: Settings, body: schemas.AskRequest) -> Iterator[bytes]:
    cfg = StreamConfig(
        k=body.k,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        include_summaries=body.include_summaries,
        summary_k=body.summary_k,
        summary_level=body.summary_level,
    )
    try:
        for ev in stream_answer(settings, body.query, config=cfg):
            if ev.kind == "retrieval" and ev.retrieval is not None:
                yield _sse(
                    "retrieval",
                    {
                        "embed_model": ev.retrieval.embed_model,
                        "chat_model": ev.retrieval.chat_model,
                        "elapsed_ms": ev.retrieval.elapsed_ms,
                        "hits": [
                            {
                                "rank": i + 1,
                                "chunk_id": h.chunk_id,
                                "file_path": h.file_path,
                                "heading_path": h.heading_path,
                                "citation_key": h.citation_key,
                                "distance": h.distance,
                                "token_count": h.token_count,
                            }
                            for i, h in enumerate(ev.retrieval.hits)
                        ],
                        "summaries": [
                            {
                                "node_id": s.node_id,
                                "level": s.level,
                                "title": s.title,
                                "summary": s.summary,
                                "member_count": s.member_count,
                                "distance": s.distance,
                            }
                            for s in ev.retrieval.summaries
                        ],
                    },
                )
            elif ev.kind == "delta" and ev.text:
                yield _sse("delta", {"text": ev.text})
            elif ev.kind == "error":
                yield _sse("error", {"text": ev.text or "stream error"})
                return
            elif ev.kind == "done":
                cited = extract_citation_keys(ev.text or "")
                yield _sse(
                    "done",
                    {
                        "text": ev.text or "",
                        "finish_reason": ev.finish_reason,
                        "cited": cited,
                    },
                )
    except VeniceAuthError as e:
        yield _sse("error", {"text": str(e)})
    except VeniceConnectionError as e:
        yield _sse("error", {"text": str(e)})
    except Exception as e:  # last-resort surface
        yield _sse("error", {"text": f"{type(e).__name__}: {e}"})


@router.post("/ask")
async def ask(body: schemas.AskRequest, request: Request, settings: Settings = Depends(get_settings)):
    """Streaming RAG answer via Server-Sent Events.

    Client closes the connection by aborting the fetch; Starlette will stop
    iterating the underlying generator.
    """
    if not settings.chunks_db.exists():
        raise HTTPException(
            status_code=409,
            detail=f"No index found at {settings.chunks_db}. Run `murano index` first.",
        )

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _ask_event_stream(settings, body),
        media_type="text/event-stream",
        headers=headers,
    )


# --- Maintenance --- ---


@router.post("/index")
def trigger_index(settings: Settings = Depends(get_settings)):
    """Trigger a full vault re-index. Synchronous (blocks until done)."""
    if not settings.vault_root.exists():
        raise HTTPException(status_code=409, detail="Vault does not exist.")
    try:
        report = index_vault(settings)
    except VeniceAuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except VeniceConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {
        "files_seen": report.files_seen,
        "files_indexed": report.files_indexed,
        "files_unchanged": report.files_unchanged,
        "files_removed": report.files_removed,
        "chunks_inserted": report.chunks_inserted,
        "elapsed_seconds": report.elapsed_seconds,
        "errors": [{"path": e.relpath, "error": e.error} for e in report.errors],
    }


@router.post("/tree/rebuild")
def trigger_tree_rebuild(settings: Settings = Depends(get_settings)):
    """Synchronously rebuild the summary tree from the current chunks index."""
    from ..tree.build import build_tree

    try:
        report = build_tree(settings)
    except VeniceAuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except VeniceConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if report.skipped_reason:
        raise HTTPException(status_code=409, detail=report.skipped_reason)
    return {
        "source_chunk_count": report.source_chunk_count,
        "total_nodes": report.total_nodes,
        "total_edges": report.total_edges,
        "embed_model": report.embed_model,
        "chat_model": report.chat_model,
        "elapsed_seconds": report.elapsed_seconds,
        "levels": [
            {
                "level": s.level,
                "inputs": s.inputs,
                "k": s.k,
                "summary_calls": s.summary_calls,
                "elapsed_seconds": s.elapsed_seconds,
            }
            for s in report.levels
        ],
    }


@router.post("/ping")
def ping(settings: Settings = Depends(get_settings)):
    """Resolve chat + embed models against Venice. Mirrors `murano ping`."""
    try:
        resolved = resolve_models(settings)
    except VeniceAuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except VeniceConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {
        "chat": {
            "requested": resolved.chat.requested,
            "resolved": resolved.chat.resolved,
            "match": resolved.chat.match,
        },
        "embed": {
            "requested": resolved.embed.requested,
            "resolved": resolved.embed.resolved,
            "match": resolved.embed.match,
            "embedding_dimensions": resolved.embed.embedding_dimensions,
            "max_input_tokens": resolved.embed.max_input_tokens,
        },
    }


@router.get("/vault/tree")
def vault_tree(settings: Settings = Depends(get_settings)):
    """Return a nested directory listing of the vault, Markdown files only."""
    vault = settings.vault_root.resolve()
    if not vault.exists():
        return {"vault_root": str(vault), "entries": []}

    def walk(d: Path) -> list[dict]:
        entries: list[dict] = []
        for child in sorted(d.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                kids = walk(child)
                if kids:
                    entries.append({"type": "dir", "name": child.name, "children": kids})
            elif child.is_file() and child.suffix.lower() in (".md", ".markdown"):
                rel = child.resolve().relative_to(vault)
                entries.append({"type": "file", "name": child.name, "path": str(rel)})
        return entries

    return {"vault_root": str(vault), "entries": walk(vault)}


@router.get("/vault/file")
def vault_file(path: str, settings: Settings = Depends(get_settings)):
    """Return the raw text of a vault-relative Markdown file."""
    candidate = (settings.vault_root / path).resolve()
    vault_root = settings.vault_root.resolve()
    if not str(candidate).startswith(str(vault_root)):
        raise HTTPException(status_code=400, detail="Path is outside the vault.")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    try:
        text = candidate.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {
        "path": path,
        "size": candidate.stat().st_size,
        "mtime": candidate.stat().st_mtime,
        "content": text,
    }
