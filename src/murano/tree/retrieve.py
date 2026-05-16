"""Tree-side retrieval helpers used by the hybrid RAG pipeline.

We don't try to do full hierarchical drill-down at retrieval time; that's
overkill given KNN over the summary nodes already returns thematically
relevant nodes directly. Instead we expose two operations:

    - search_summaries(query_vec, k, level): top-K summary nodes (optionally
      restricted to a level), used to enrich the RAG prompt.
    - get_chunk(chunk_id): direct chunk lookup, used by the MCP `get_chunk`
      tool and by callers that already know an id.

True drill-down (root -> level N -> ... -> leaf chunks) is a Phase-5.5
enhancement; for now hybrid prompts use `search_summaries(level=1)` to
inject the most relevant themes alongside flat-search chunk citations.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..config import Settings
from ..index import db as chunks_db
from . import db as tree_db


@dataclass
class ChunkRecord:
    """Full chunk record returned from `get_chunk`."""

    chunk_id: str
    file_path: str
    ord: int
    heading_path: str
    content: str
    token_count: int
    byte_offset: int


def tree_exists(settings: Settings) -> bool:
    if not settings.summary_tree_db.exists():
        return False
    conn = tree_db.connect(settings.summary_tree_db)
    try:
        return tree_db.has_tree(conn)
    finally:
        conn.close()


def search_top_summaries(
    settings: Settings,
    query_embedding: Sequence[float],
    *,
    k: int = 2,
    level: int = 1,
) -> list[tree_db.SummaryHit]:
    """KNN over summary nodes at a given level (default: leaf-most summaries, L1)."""
    if not settings.summary_tree_db.exists():
        return []
    conn = tree_db.connect(settings.summary_tree_db)
    try:
        if not tree_db.has_tree(conn):
            return []
        return tree_db.search_summaries(conn, query_embedding, level=level, k=k)
    finally:
        conn.close()


def list_themes(settings: Settings, *, level: int = 1) -> list[tree_db.TreeNode]:
    """All summary nodes at a level, ordered by id. Returns [] if no tree."""
    if not settings.summary_tree_db.exists():
        return []
    conn = tree_db.connect(settings.summary_tree_db)
    try:
        if not tree_db.has_tree(conn):
            return []
        return tree_db.list_nodes_at_level(conn, level)
    finally:
        conn.close()


def get_chunk(settings: Settings, chunk_id: str) -> ChunkRecord | None:
    """Fetch a single chunk by id from chunks.db."""
    if not settings.chunks_db.exists():
        return None
    conn = chunks_db.connect(settings.chunks_db)
    try:
        row = conn.execute(
            """
            SELECT id, file_path, ord, heading_path, content, token_count, byte_offset
            FROM chunks WHERE id = ?
            """,
            (chunk_id,),
        ).fetchone()
        if not row:
            return None
        return ChunkRecord(
            chunk_id=row["id"],
            file_path=row["file_path"],
            ord=row["ord"],
            heading_path=row["heading_path"],
            content=row["content"],
            token_count=row["token_count"],
            byte_offset=row["byte_offset"],
        )
    finally:
        conn.close()


@dataclass
class TreeStatus:
    """Health check for the summary tree (used by CLI/stale detection)."""

    exists: bool
    node_count: int
    level_count: int
    levels: list[int]
    embed_model: str | None
    chat_model: str | None
    built_at: int | None
    source_chunk_count: int | None
    current_chunk_count: int
    is_stale: bool
    stale_reason: str | None


def status(settings: Settings) -> TreeStatus:
    """Cheap health snapshot: tree presence + drift vs. current chunks count."""
    current_chunks = 0
    if settings.chunks_db.exists():
        cconn = chunks_db.connect(settings.chunks_db)
        try:
            current_chunks = chunks_db.chunk_count(cconn)
        finally:
            cconn.close()

    if not settings.summary_tree_db.exists():
        return TreeStatus(
            exists=False,
            node_count=0,
            level_count=0,
            levels=[],
            embed_model=None,
            chat_model=None,
            built_at=None,
            source_chunk_count=None,
            current_chunk_count=current_chunks,
            is_stale=current_chunks > 0,
            stale_reason="No tree built yet." if current_chunks > 0 else None,
        )

    tconn = tree_db.connect(settings.summary_tree_db)
    try:
        if not tree_db.has_tree(tconn):
            return TreeStatus(
                exists=False,
                node_count=0,
                level_count=0,
                levels=[],
                embed_model=None,
                chat_model=None,
                built_at=None,
                source_chunk_count=None,
                current_chunk_count=current_chunks,
                is_stale=current_chunks > 0,
                stale_reason="Tree DB exists but is empty." if current_chunks > 0 else None,
            )
        built_at = int(tree_db.get_meta(tconn, "built_at") or "0")
        source = int(tree_db.get_meta(tconn, "source_chunk_count") or "0")
        embed_model = tree_db.get_meta(tconn, "embed_model")
        chat_model = tree_db.get_meta(tconn, "chat_model")
        levels = tree_db.list_levels(tconn)

        drift = abs(current_chunks - source) / max(source, 1)
        is_stale = drift > 0.10 or current_chunks < source * 0.5
        stale_reason = None
        if is_stale:
            stale_reason = (
                f"Tree built from {source} chunks but the index now has "
                f"{current_chunks}. Run `murano tree rebuild` to refresh."
            )

        return TreeStatus(
            exists=True,
            node_count=tree_db.node_count(tconn),
            level_count=int(tree_db.get_meta(tconn, "level_count") or "0"),
            levels=levels,
            embed_model=embed_model,
            chat_model=chat_model,
            built_at=built_at,
            source_chunk_count=source,
            current_chunk_count=current_chunks,
            is_stale=is_stale,
            stale_reason=stale_reason,
        )
    finally:
        tconn.close()
