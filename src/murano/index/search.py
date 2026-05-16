"""Top-level search helper — used by the `murano search` debug command and Phase 3 RAG."""

from __future__ import annotations

from ..config import Settings
from ..venice import build_client, resolve_models
from . import db as dbmod
from .embed import embed_one


def search(settings: Settings, query: str, k: int = 10) -> list[dbmod.SearchHit]:
    """Embed a query and return the top-k chunks ranked by vector distance."""
    client = build_client(settings)
    resolved = resolve_models(settings)
    query_vec = embed_one(client, resolved.embed.resolved, query)

    conn = dbmod.connect(settings.chunks_db)
    try:
        dbmod.init_for_model(
            conn,
            resolved.embed.resolved,
            resolved.embed.embedding_dimensions or len(query_vec),
        )
        return dbmod.search(conn, query_vec, k=k)
    finally:
        conn.close()
