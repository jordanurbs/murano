"""Summary tree storage (`summary_tree.db`).

Schema:

    meta(key PK, value)
        — schema_version, embed_model, embed_dims, chat_model,
          built_at (unix ts), source_chunk_count, level_count

    tree_nodes(id PK, level, title, summary, member_count,
               parent_id FK -> tree_nodes(id) ON DELETE SET NULL,
               created_at)
        — one row per summary node. id format: "L<level>::<seq>".

    tree_edges(parent_id, child_id, child_level, PRIMARY KEY (parent_id, child_id))
        — child_id is a chunk_id when child_level == 0, else another tree_nodes.id.

    vec_tree_nodes(rowid, embedding FLOAT[<dims>])
        — sqlite-vec virtual table mirroring tree_nodes embeddings, joined
          on tree_nodes.rowid the same way chunks/vec_chunks are paired.

Lifecycle: the builder calls `rebuild()` which atomically wipes and rewrites
everything. Read paths (search/walk) are cheap and don't need transactions.
"""

from __future__ import annotations

import sqlite3
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec

SCHEMA_VERSION = "1"


@dataclass
class TreeNodeRow:
    """One node ready to be persisted (with its embedding)."""

    id: str
    level: int
    title: str
    summary: str
    member_count: int
    parent_id: str | None
    embedding: list[float]


@dataclass
class TreeNode:
    """One node read back from the DB."""

    rowid: int
    id: str
    level: int
    title: str
    summary: str
    member_count: int
    parent_id: str | None


@dataclass
class SummaryHit:
    """A summary node returned from a KNN query."""

    node_id: str
    level: int
    title: str
    summary: str
    member_count: int
    distance: float


def _serialize_embedding(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _create_base_schema(conn)
    return conn


def _create_base_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tree_nodes (
            id           TEXT PRIMARY KEY,
            level        INTEGER NOT NULL,
            title        TEXT NOT NULL DEFAULT '',
            summary      TEXT NOT NULL,
            member_count INTEGER NOT NULL DEFAULT 0,
            parent_id    TEXT REFERENCES tree_nodes(id) ON DELETE SET NULL,
            created_at   REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tree_nodes_level ON tree_nodes(level);
        CREATE INDEX IF NOT EXISTS idx_tree_nodes_parent ON tree_nodes(parent_id);

        CREATE TABLE IF NOT EXISTS tree_edges (
            parent_id   TEXT NOT NULL,
            child_id    TEXT NOT NULL,
            child_level INTEGER NOT NULL,
            PRIMARY KEY (parent_id, child_id)
        );

        CREATE INDEX IF NOT EXISTS idx_tree_edges_child ON tree_edges(child_id);
        """
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def _vec_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table','view') AND name = 'vec_tree_nodes'"
    ).fetchone()
    return row is not None


def _ensure_vec_table(conn: sqlite3.Connection, embed_dims: int) -> None:
    if _vec_table_exists(conn):
        conn.execute("DROP TABLE vec_tree_nodes")
    conn.execute(
        f"CREATE VIRTUAL TABLE vec_tree_nodes USING vec0(embedding FLOAT[{embed_dims}])"
    )
    conn.commit()


def rebuild(
    conn: sqlite3.Connection,
    *,
    nodes: list[TreeNodeRow],
    edges: list[tuple[str, str, int]],
    embed_model: str,
    embed_dims: int,
    chat_model: str,
    source_chunk_count: int,
) -> None:
    """Atomically replace the entire tree contents."""
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM tree_edges")
        conn.execute("DELETE FROM tree_nodes")
        # Re-create vec table to match dims (cheap; nothing to migrate).
        if _vec_table_exists(conn):
            conn.execute("DROP TABLE vec_tree_nodes")
        conn.execute(
            f"CREATE VIRTUAL TABLE vec_tree_nodes USING vec0(embedding FLOAT[{embed_dims}])"
        )

        # Insert nodes level-by-level so FKs resolve.
        nodes_by_level: dict[int, list[TreeNodeRow]] = {}
        for n in nodes:
            nodes_by_level.setdefault(n.level, []).append(n)

        for level in sorted(nodes_by_level.keys(), reverse=True):  # highest level first
            for node in nodes_by_level[level]:
                cur = conn.execute(
                    """
                    INSERT INTO tree_nodes(id, level, title, summary, member_count, parent_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node.id,
                        node.level,
                        node.title,
                        node.summary,
                        node.member_count,
                        node.parent_id,
                        time.time(),
                    ),
                )
                conn.execute(
                    "INSERT INTO vec_tree_nodes(rowid, embedding) VALUES (?, ?)",
                    (cur.lastrowid, _serialize_embedding(node.embedding)),
                )

        for parent_id, child_id, child_level in edges:
            conn.execute(
                "INSERT OR IGNORE INTO tree_edges(parent_id, child_id, child_level) VALUES (?, ?, ?)",
                (parent_id, child_id, child_level),
            )

        # Update meta.
        now = str(int(time.time()))
        levels = sorted(nodes_by_level.keys())
        for k, v in (
            ("schema_version", SCHEMA_VERSION),
            ("embed_model", embed_model),
            ("embed_dims", str(embed_dims)),
            ("chat_model", chat_model),
            ("built_at", now),
            ("source_chunk_count", str(source_chunk_count)),
            ("level_count", str(len(levels))),
            ("levels", ",".join(str(lvl) for lvl in levels)),
        ):
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, v),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise


def has_tree(conn: sqlite3.Connection) -> bool:
    return get_meta(conn, "built_at") is not None and node_count(conn) > 0


def node_count(conn: sqlite3.Connection, level: int | None = None) -> int:
    if level is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM tree_nodes").fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM tree_nodes WHERE level = ?", (level,)).fetchone()
    return row["n"] if row else 0


def list_levels(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT DISTINCT level FROM tree_nodes ORDER BY level").fetchall()
    return [r["level"] for r in rows]


def list_nodes_at_level(conn: sqlite3.Connection, level: int) -> list[TreeNode]:
    rows = conn.execute(
        """
        SELECT rowid, id, level, title, summary, member_count, parent_id
        FROM tree_nodes WHERE level = ? ORDER BY id
        """,
        (level,),
    ).fetchall()
    return [
        TreeNode(
            rowid=r["rowid"],
            id=r["id"],
            level=r["level"],
            title=r["title"],
            summary=r["summary"],
            member_count=r["member_count"],
            parent_id=r["parent_id"],
        )
        for r in rows
    ]


def get_node(conn: sqlite3.Connection, node_id: str) -> TreeNode | None:
    row = conn.execute(
        """
        SELECT rowid, id, level, title, summary, member_count, parent_id
        FROM tree_nodes WHERE id = ?
        """,
        (node_id,),
    ).fetchone()
    if not row:
        return None
    return TreeNode(
        rowid=row["rowid"],
        id=row["id"],
        level=row["level"],
        title=row["title"],
        summary=row["summary"],
        member_count=row["member_count"],
        parent_id=row["parent_id"],
    )


def get_children_ids(
    conn: sqlite3.Connection, parent_id: str
) -> list[tuple[str, int]]:
    """Return [(child_id, child_level), ...] for a given parent node."""
    rows = conn.execute(
        "SELECT child_id, child_level FROM tree_edges WHERE parent_id = ? ORDER BY child_id",
        (parent_id,),
    ).fetchall()
    return [(r["child_id"], r["child_level"]) for r in rows]


def search_summaries(
    conn: sqlite3.Connection,
    query_embedding: Sequence[float],
    *,
    level: int | None = None,
    k: int = 3,
) -> list[SummaryHit]:
    """KNN over vec_tree_nodes, optionally filtered to a single level."""
    if level is None:
        rows = conn.execute(
            """
            SELECT n.id AS node_id, n.level AS level, n.title AS title,
                   n.summary AS summary, n.member_count AS member_count,
                   v.distance AS distance
            FROM vec_tree_nodes v
            JOIN tree_nodes n ON n.rowid = v.rowid
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
            """,
            (_serialize_embedding(query_embedding), k),
        ).fetchall()
    else:
        # `level` filter has to happen client-side because vec0 MATCH must be
        # the only predicate on the embedding column. We over-fetch then filter.
        over_k = max(k * 4, k + 5)
        rows = conn.execute(
            """
            SELECT n.id AS node_id, n.level AS level, n.title AS title,
                   n.summary AS summary, n.member_count AS member_count,
                   v.distance AS distance
            FROM vec_tree_nodes v
            JOIN tree_nodes n ON n.rowid = v.rowid
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
            """,
            (_serialize_embedding(query_embedding), over_k),
        ).fetchall()
        rows = [r for r in rows if r["level"] == level][:k]
    return [
        SummaryHit(
            node_id=r["node_id"],
            level=r["level"],
            title=r["title"],
            summary=r["summary"],
            member_count=r["member_count"],
            distance=r["distance"],
        )
        for r in rows
    ]
