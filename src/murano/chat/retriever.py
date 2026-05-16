"""Shared retrieval core.

Used by:
- `murano ask`   (CLI, Phase 3)
- `murano mcp`   (MCP server, Phase 3.5)
- `/api/v1/ask`  (HTTP, Phase 6)
- `/api/v1/search` (HTTP, Phase 6)

A `Retriever` owns one Venice client + one sqlite-vec connection for the
lifetime of an operation. For long-lived servers, you'd construct a Retriever
per process and reuse it; for one-shot CLI calls, use the context manager.

Citation keys are Obsidian-style: `<relpath-without-.md>#<leaf-heading>`.
For example, a chunk at `cooking/risotto.md` under heading path
`Mushroom Risotto > Method` produces citation key `cooking/risotto#Method`.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..config import Settings
from ..index import db as dbmod
from ..index.embed import embed_one
from ..tree import db as tree_db
from ..vault.chunker import HEADING_SEPARATOR
from ..venice import ResolvedModels, build_client, resolve_models

if TYPE_CHECKING:
    from openai import OpenAI


@dataclass
class RetrievedChunk:
    """One chunk surfaced by the retriever, enriched with a citation key."""

    chunk_id: str
    file_path: str
    heading_path: str
    content: str
    token_count: int
    distance: float
    citation_key: str  # e.g. "cooking/risotto#Method"


@dataclass
class RetrievedSummary:
    """One summary node surfaced by hybrid retrieval (context, not a citation)."""

    node_id: str
    level: int
    title: str
    summary: str
    member_count: int
    distance: float


@dataclass
class RetrievalResult:
    """The output of a single retrieve() call."""

    query: str
    hits: list[RetrievedChunk]
    embed_model: str
    chat_model: str
    embed_dims: int | None
    elapsed_ms: float
    summaries: list[RetrievedSummary] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.summaries is None:
            self.summaries = []


def derive_citation_key(file_path: str, heading_path: str) -> str:
    """Build an Obsidian-style `[[file#heading]]` body from a chunk's metadata.

    The file part drops the `.md`/`.markdown` extension. The heading part is
    the deepest segment of the heading_path (or "" if the chunk has no heading).
    """
    base = file_path
    for ext in (".md", ".markdown"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    leaf = ""
    if heading_path:
        parts = [p.strip() for p in heading_path.split(HEADING_SEPARATOR) if p.strip()]
        if parts:
            leaf = parts[-1]
    return f"{base}#{leaf}" if leaf else base


class Retriever:
    """Shared retrieval object — owns a Venice client + a sqlite-vec connection.

    Always use via the `open()` context manager so the SQLite connection is
    closed cleanly:

        with Retriever.open(settings) as r:
            result = r.retrieve("how does X work", k=6)
            # result.hits, result.chat_model, etc.
    """

    def __init__(
        self,
        settings: Settings,
        client: OpenAI,
        resolved: ResolvedModels,
        conn,  # sqlite3.Connection
        tree_conn=None,  # sqlite3.Connection | None
    ) -> None:
        self.settings = settings
        self.client = client
        self.resolved = resolved
        self.conn = conn
        self.tree_conn = tree_conn

    @classmethod
    @contextmanager
    def open(cls, settings: Settings):
        """Construct a Retriever, set up the DB for the active embed model, and clean up."""
        client = build_client(settings)
        resolved = resolve_models(settings)
        if resolved.embed.embedding_dimensions is None:
            raise RuntimeError(
                f"Embedding model '{resolved.embed.resolved}' did not report dimensions; "
                "cannot open the vector index."
            )
        conn = dbmod.connect(settings.chunks_db)
        tconn: sqlite3.Connection | None = None
        if settings.summary_tree_db.exists():
            tconn = tree_db.connect(settings.summary_tree_db)
            if not tree_db.has_tree(tconn):
                tconn.close()
                tconn = None
        try:
            dbmod.init_for_model(conn, resolved.embed.resolved, resolved.embed.embedding_dimensions)
            yield cls(
                settings=settings,
                client=client,
                resolved=resolved,
                conn=conn,
                tree_conn=tconn,
            )
        finally:
            conn.close()
            if tconn is not None:
                tconn.close()

    def retrieve(
        self,
        query: str,
        k: int = 6,
        *,
        include_summaries: bool = True,
        summary_k: int = 2,
        summary_level: int = 1,
    ) -> RetrievalResult:
        """Embed the query and return the top-k chunks (and optionally top summaries).

        When `include_summaries=True` AND a summary tree exists in this settings'
        data dir, also pulls `summary_k` summary nodes at `summary_level`. The
        same query embedding is reused for both lookups — one network call total.
        """
        started = time.monotonic()
        query_vec = embed_one(self.client, self.resolved.embed.resolved, query)
        raw_hits = dbmod.search(self.conn, query_vec, k=k)

        summaries: list[RetrievedSummary] = []
        if include_summaries and self.tree_conn is not None and summary_k > 0:
            raw_summaries = tree_db.search_summaries(
                self.tree_conn, query_vec, level=summary_level, k=summary_k
            )
            summaries = [
                RetrievedSummary(
                    node_id=s.node_id,
                    level=s.level,
                    title=s.title,
                    summary=s.summary,
                    member_count=s.member_count,
                    distance=s.distance,
                )
                for s in raw_summaries
            ]

        elapsed_ms = (time.monotonic() - started) * 1000.0

        hits = [
            RetrievedChunk(
                chunk_id=h.chunk_id,
                file_path=h.file_path,
                heading_path=h.heading_path,
                content=h.content,
                token_count=h.token_count,
                distance=h.distance,
                citation_key=derive_citation_key(h.file_path, h.heading_path),
            )
            for h in raw_hits
        ]

        return RetrievalResult(
            query=query,
            hits=hits,
            embed_model=self.resolved.embed.resolved,
            chat_model=self.resolved.chat.resolved,
            embed_dims=self.resolved.embed.embedding_dimensions,
            elapsed_ms=elapsed_ms,
            summaries=summaries,
        )
