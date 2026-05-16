"""Tree-build pipeline.

Pipeline per level L >= 1:
    1. Load items (chunks at L=1, summary nodes at L>1) with embeddings.
    2. If items <= min_cluster_size, stop building further levels.
    3. K = recommend_k(n_items). Run kmeans -> labels per item.
    4. For each cluster c:
         - Collect member texts (excerpts).
         - LLM-summarize (title + summary).
         - Embed the new summary node.
         - Record TreeNodeRow + edges from this node to its children.
    5. Use the new summary nodes as input to L+1.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from ..config import Settings
from ..index import db as chunks_db
from ..index.embed import embed_texts
from ..venice import build_client, resolve_models
from . import cluster as cluster_mod
from . import db as tree_db
from .summarize import summarize_cluster

DEFAULT_MAX_LEVELS = 3
DEFAULT_MIN_CLUSTER_SIZE = 5  # stop building when fewer than this many items remain
DEFAULT_EMBED_BATCH = 32


@dataclass
class LevelStats:
    level: int
    inputs: int
    k: int
    summary_calls: int
    elapsed_seconds: float


@dataclass
class BuildReport:
    source_chunk_count: int
    embed_model: str
    chat_model: str
    embed_dims: int
    levels: list[LevelStats] = field(default_factory=list)
    total_nodes: int = 0
    total_edges: int = 0
    elapsed_seconds: float = 0.0
    skipped_reason: str | None = None  # if we couldn't build at all


@dataclass
class _Item:
    """A clusterable item — either a chunk (level 0) or a summary node (level >= 1)."""

    id: str
    text: str
    embedding: list[float]
    child_level: int  # 0 if this is a chunk, otherwise this item's own level


def _load_chunk_items(conn: sqlite3.Connection) -> list[_Item]:
    """Read every chunk + its embedding from the chunks DB."""
    rows = conn.execute(
        """
        SELECT c.id AS chunk_id, c.content AS content, v.embedding AS embedding
        FROM chunks c
        JOIN vec_chunks v ON c.rowid = v.rowid
        """
    ).fetchall()
    items: list[_Item] = []
    for r in rows:
        emb_bytes = r["embedding"]
        # vec0 stores little-endian float32; unpack to list of floats.
        n = len(emb_bytes) // 4
        emb = list(np.frombuffer(emb_bytes, dtype="<f4", count=n))
        emb = [float(x) for x in emb]
        items.append(
            _Item(
                id=r["chunk_id"],
                text=r["content"],
                embedding=emb,
                child_level=0,
            )
        )
    return items


def _stack_embeddings(items: list[_Item]) -> np.ndarray:
    return np.asarray([it.embedding for it in items], dtype=np.float64)


def _build_one_level(
    *,
    items: list[_Item],
    level: int,
    client,
    chat_model: str,
    embed_model: str,
    seed: int,
    progress: Callable[[str], None] | None,
    settings: Settings,
) -> tuple[list[_Item], list[tree_db.TreeNodeRow], list[tuple[str, str, int]], LevelStats]:
    """Cluster `items` and produce summary nodes at `level`. Returns (new_items, nodes, edges, stats)."""
    started = time.monotonic()
    n = len(items)
    k = cluster_mod.recommend_k(n)
    if k == 0 or k > n:
        return (
            [],
            [],
            [],
            LevelStats(level=level, inputs=n, k=0, summary_calls=0, elapsed_seconds=0.0),
        )

    X = _stack_embeddings(items)
    result = cluster_mod.kmeans(X, k=k, seed=seed)
    labels = result.labels

    # Group items by cluster id.
    clusters: dict[int, list[_Item]] = {}
    for idx, lab in enumerate(labels):
        clusters.setdefault(int(lab), []).append(items[idx])

    if progress:
        progress(f"  Level {level}: clustered {n} -> {len(clusters)} clusters")

    nodes_to_persist: list[tree_db.TreeNodeRow] = []
    edges: list[tuple[str, str, int]] = []
    summary_calls = 0
    new_items_for_next_level: list[_Item] = []

    summary_texts: list[str] = []
    pending_node_ids: list[str] = []
    pending_clusters: list[list[_Item]] = []

    for cluster_idx, members in clusters.items():
        if not members:
            continue
        node_id = f"L{level}::{cluster_idx}"
        if progress:
            progress(f"    Summarizing {node_id} ({len(members)} members)...")
        summarization = summarize_cluster(
            client,
            chat_model=chat_model,
            member_texts=[m.text for m in members],
            usage_log_dir=settings.data_root,
        )
        summary_calls += 1

        summary_texts.append(f"{summarization.title}. {summarization.summary}")
        pending_node_ids.append(node_id)
        pending_clusters.append(members)

        # Record node + edges (embedding filled in below).
        nodes_to_persist.append(
            tree_db.TreeNodeRow(
                id=node_id,
                level=level,
                title=summarization.title,
                summary=summarization.summary,
                member_count=len(members),
                parent_id=None,  # set in outer loop when level+1 clusters us
                embedding=[],  # placeholder, replaced after batch-embed below
            )
        )
        for member in members:
            edges.append((node_id, member.id, member.child_level))

    if pending_node_ids:
        if progress:
            progress(f"    Embedding {len(summary_texts)} summary nodes...")
        embeddings = embed_texts(
            client,
            embed_model,
            summary_texts,
            batch_size=DEFAULT_EMBED_BATCH,
            usage_log_dir=settings.data_root,
            operation="embed-summary",
        )
        if len(embeddings) != len(pending_node_ids):
            raise RuntimeError(
                f"Embedding count mismatch: expected {len(pending_node_ids)}, "
                f"got {len(embeddings)}"
            )
        for node, emb in zip(nodes_to_persist, embeddings, strict=True):
            node.embedding = emb
            new_items_for_next_level.append(
                _Item(
                    id=node.id,
                    text=f"{node.title}. {node.summary}",
                    embedding=emb,
                    child_level=level,
                )
            )

    return (
        new_items_for_next_level,
        nodes_to_persist,
        edges,
        LevelStats(
            level=level,
            inputs=n,
            k=k,
            summary_calls=summary_calls,
            elapsed_seconds=time.monotonic() - started,
        ),
    )


def build_tree(
    settings: Settings,
    *,
    max_levels: int = DEFAULT_MAX_LEVELS,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    seed: int = 0,
    progress: Callable[[str], None] | None = None,
) -> BuildReport:
    """Build (or rebuild) the summary tree from `chunks.db` into `summary_tree.db`."""
    started = time.monotonic()

    resolved = resolve_models(settings)
    if resolved.embed.embedding_dimensions is None:
        raise RuntimeError(
            f"Embedding model '{resolved.embed.resolved}' did not report dimensions; "
            "cannot build the summary tree."
        )

    if not settings.chunks_db.exists():
        return BuildReport(
            source_chunk_count=0,
            embed_model=resolved.embed.resolved,
            chat_model=resolved.chat.resolved,
            embed_dims=resolved.embed.embedding_dimensions,
            skipped_reason=(
                f"No chunks index found at {settings.chunks_db}. "
                "Run `murano index` first."
            ),
            elapsed_seconds=time.monotonic() - started,
        )

    chunks_conn = chunks_db.connect(settings.chunks_db)
    try:
        all_items = _load_chunk_items(chunks_conn)
    finally:
        chunks_conn.close()

    report = BuildReport(
        source_chunk_count=len(all_items),
        embed_model=resolved.embed.resolved,
        chat_model=resolved.chat.resolved,
        embed_dims=resolved.embed.embedding_dimensions,
    )

    if len(all_items) < min_cluster_size:
        report.skipped_reason = (
            f"Only {len(all_items)} chunks in the index — need at least "
            f"{min_cluster_size} to build a useful tree."
        )
        report.elapsed_seconds = time.monotonic() - started
        return report

    client = build_client(settings)
    all_nodes: list[tree_db.TreeNodeRow] = []
    all_edges: list[tuple[str, str, int]] = []
    current_items = all_items

    for level in range(1, max_levels + 1):
        if len(current_items) < min_cluster_size:
            if progress:
                progress(
                    f"  Stopping at level {level} — only {len(current_items)} items remain "
                    f"(< min_cluster_size={min_cluster_size})."
                )
            break
        new_items, nodes, edges, stats = _build_one_level(
            items=current_items,
            level=level,
            client=client,
            chat_model=resolved.chat.resolved,
            embed_model=resolved.embed.resolved,
            seed=seed + level,
            progress=progress,
            settings=settings,
        )
        if not nodes:
            if progress:
                progress(f"  Level {level} produced no clusters; stopping.")
            break
        report.levels.append(stats)
        all_nodes.extend(nodes)
        all_edges.extend(edges)
        current_items = new_items

    # Backfill parent_id on level-N nodes from edges produced by level-N+1 builds.
    parent_of: dict[str, str] = {}
    for parent_id, child_id, child_level in all_edges:
        if child_level >= 1:
            parent_of[child_id] = parent_id
    for node in all_nodes:
        if node.id in parent_of:
            node.parent_id = parent_of[node.id]

    if not all_nodes:
        report.skipped_reason = "Clustering produced no nodes (vault too small)."
        report.elapsed_seconds = time.monotonic() - started
        return report

    tree_conn = tree_db.connect(settings.summary_tree_db)
    try:
        tree_db.rebuild(
            tree_conn,
            nodes=all_nodes,
            edges=all_edges,
            embed_model=resolved.embed.resolved,
            embed_dims=resolved.embed.embedding_dimensions,
            chat_model=resolved.chat.resolved,
            source_chunk_count=len(all_items),
        )
    finally:
        tree_conn.close()

    report.total_nodes = len(all_nodes)
    report.total_edges = len(all_edges)
    report.elapsed_seconds = time.monotonic() - started
    return report
