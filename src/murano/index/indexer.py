"""Vault indexer — orchestrates: walk → hash → chunk → embed → persist."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..vault.chunker import (
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_TARGET_TOKENS,
    Chunk,
    chunk_markdown,
    file_hash,
)
from ..venice import VeniceAuthError, build_client, resolve_models
from . import db as dbmod
from .embed import embed_texts

MARKDOWN_GLOBS: tuple[str, ...] = ("*.md", "*.markdown")


@dataclass
class FileResult:
    relpath: str
    chunks: int
    status: str  # "indexed" | "unchanged" | "removed" | "error"
    error: str | None = None


@dataclass
class IndexReport:
    files_seen: int = 0
    files_indexed: int = 0
    files_unchanged: int = 0
    files_removed: int = 0
    chunks_inserted: int = 0
    embedding_calls: int = 0
    embed_dims: int | None = None
    embed_model: str | None = None
    chat_model: str | None = None
    errors: list[FileResult] = field(default_factory=list)
    elapsed_seconds: float = 0.0


def iter_vault_files(vault_root: Path, subpath: Path | None = None) -> Iterable[Path]:
    """Yield Markdown files under the vault, optionally restricted to subpath.

    Hidden directories (starting with ".") are skipped. Symlinks are followed
    only one level (we resolve and then proceed); we don't try to detect cycles.
    """
    base = (vault_root / subpath).resolve() if subpath else vault_root.resolve()
    if not base.exists():
        return
    if base.is_file():
        if any(base.match(g) for g in MARKDOWN_GLOBS):
            yield base
        return
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(vault_root).parts):
            continue
        if not any(path.match(g) for g in MARKDOWN_GLOBS):
            continue
        yield path


def _relpath(vault_root: Path, p: Path) -> str:
    try:
        return str(p.resolve().relative_to(vault_root.resolve()))
    except ValueError:
        return str(p)


def index_vault(
    settings: Settings,
    *,
    subpath: Path | None = None,
    force: bool = False,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    progress: Callable[[FileResult], None] | None = None,
) -> IndexReport:
    """Walk the vault and bring `chunks.db` into sync.

    Args:
        settings: loaded Murano settings.
        subpath:  if set, only index this vault-relative subdirectory; pruning
                  of deleted files is also scoped to that subpath.
        force:    re-embed every file even if the file hash matches.
        progress: optional callback fired once per file processed.
    """
    started = time.monotonic()
    report = IndexReport()

    try:
        client = build_client(settings)
    except VeniceAuthError:
        raise

    resolved = resolve_models(settings)
    if resolved.embed.embedding_dimensions is None:
        raise RuntimeError(
            f"Embedding model '{resolved.embed.resolved}' did not report dimensions; "
            "cannot create the vector index. Pick a different embed model with "
            "MURANO_EMBED_MODEL or by editing ~/.murano/config.toml."
        )

    report.embed_model = resolved.embed.resolved
    report.chat_model = resolved.chat.resolved
    report.embed_dims = resolved.embed.embedding_dimensions

    conn = dbmod.connect(settings.chunks_db)
    try:
        rebuilt = dbmod.init_for_model(
            conn, resolved.embed.resolved, resolved.embed.embedding_dimensions
        )
        if rebuilt:
            report.files_unchanged = 0  # any prior cache was invalidated

        scope_path = (settings.vault_root / subpath).resolve() if subpath else settings.vault_root.resolve()
        seen_relpaths: set[str] = set()

        for fpath in iter_vault_files(settings.vault_root, subpath):
            report.files_seen += 1
            relpath = _relpath(settings.vault_root, fpath)
            seen_relpaths.add(relpath)

            try:
                raw = fpath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                fr = FileResult(relpath=relpath, chunks=0, status="error", error=str(e))
                report.errors.append(fr)
                if progress:
                    progress(fr)
                continue

            new_hash = file_hash(raw)
            mtime = fpath.stat().st_mtime
            existing = dbmod.get_file_record(conn, relpath)

            if not force and existing and existing["file_hash"] == new_hash:
                report.files_unchanged += 1
                fr = FileResult(relpath=relpath, chunks=existing["chunk_count"], status="unchanged")
                if progress:
                    progress(fr)
                continue

            chunks = chunk_markdown(raw, target_tokens=target_tokens, overlap_tokens=overlap_tokens)
            if not chunks:
                dbmod.delete_file(conn, relpath)
                report.files_indexed += 1
                fr = FileResult(relpath=relpath, chunks=0, status="indexed")
                if progress:
                    progress(fr)
                continue

            embeddings = embed_texts(client, resolved.embed.resolved, [c.content for c in chunks])
            report.embedding_calls += 1
            if len(embeddings) != len(chunks):
                fr = FileResult(
                    relpath=relpath,
                    chunks=0,
                    status="error",
                    error=f"Venice returned {len(embeddings)} embeddings for {len(chunks)} chunks",
                )
                report.errors.append(fr)
                if progress:
                    progress(fr)
                continue

            rows = [
                dbmod.ChunkRow(
                    id=f"{relpath}::{c.ord}",
                    file_path=relpath,
                    ord=c.ord,
                    heading_path=c.heading_path,
                    content=c.content,
                    content_hash=c.content_hash,
                    token_count=c.token_count,
                    byte_offset=c.byte_offset,
                    embedding=emb,
                )
                for c, emb in zip(chunks, embeddings, strict=True)
            ]
            inserted = dbmod.upsert_file_with_chunks(
                conn,
                file_path=relpath,
                mtime=mtime,
                file_hash=new_hash,
                indexed_at=time.time(),
                chunks=rows,
            )
            report.chunks_inserted += inserted
            report.files_indexed += 1
            fr = FileResult(relpath=relpath, chunks=inserted, status="indexed")
            if progress:
                progress(fr)

        # Prune any files that vanished from the vault (within the index scope).
        scope_rel = _relpath(settings.vault_root, scope_path)
        scope_prefix = "" if scope_rel in (".", "") else scope_rel.rstrip("/") + "/"
        for known in dbmod.list_file_paths(conn):
            if scope_prefix and not (known == scope_rel or known.startswith(scope_prefix)):
                continue
            if known in seen_relpaths:
                continue
            removed = dbmod.delete_file(conn, known)
            if removed:
                report.files_removed += 1
                fr = FileResult(relpath=known, chunks=removed, status="removed")
                if progress:
                    progress(fr)
    finally:
        conn.close()

    report.elapsed_seconds = time.monotonic() - started
    return report


def reindex_vault(settings: Settings, **kwargs) -> IndexReport:
    """Drop everything and rebuild from scratch."""
    if settings.chunks_db.exists():
        settings.chunks_db.unlink()
    return index_vault(settings, force=True, **kwargs)


def index_single_chunk_record(c: Chunk, file_relpath: str, embedding: list[float]) -> dbmod.ChunkRow:
    """Test helper: convert a Chunk + embedding into a persistable ChunkRow."""
    return dbmod.ChunkRow(
        id=f"{file_relpath}::{c.ord}",
        file_path=file_relpath,
        ord=c.ord,
        heading_path=c.heading_path,
        content=c.content,
        content_hash=c.content_hash,
        token_count=c.token_count,
        byte_offset=c.byte_offset,
        embedding=embedding,
    )
