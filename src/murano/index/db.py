"""SQLite + sqlite-vec storage.

Schema:

    files(path PK, mtime, file_hash, indexed_at, chunk_count)
        — one row per Markdown file in the vault. Used for fast skip on unchanged files.

    chunks(id PK, file_path FK, ord, heading_path, content, content_hash, token_count, byte_offset)
        — one row per logical chunk. `id` is `<file_relpath>::<ord>` for stable references.

    vec_chunks(rowid, embedding FLOAT[<dims>])
        — sqlite-vec virtual table mirroring chunk embeddings.
        — rowid corresponds to chunks.rowid (1:1) so we can JOIN on rowid for retrieval.

    meta(key PK, value)
        — schema_version, embed_model, embed_dims (so we can detect dim mismatches on reopen).

Lifecycle:
    - connect()        : open DB, load sqlite-vec extension, create schema if missing.
    - init_for_model() : record embed_model + embed_dims; rebuild vec table if dims changed.
    - upsert_file()    : insert/replace file row + delete its old chunks + their embeddings.
    - insert_chunks()  : bulk-insert chunks + matching embeddings inside a single transaction.
    - search()         : KNN over vec_chunks JOINed to chunks for content.
"""

from __future__ import annotations

import sqlite3
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlite_vec

SCHEMA_VERSION = "1"


@dataclass
class ChunkRow:
    """A chunk ready to be persisted. Embedding is a list[float] of length embed_dims."""

    id: str
    file_path: str
    ord: int
    heading_path: str
    content: str
    content_hash: str
    token_count: int
    byte_offset: int
    embedding: list[float]


@dataclass
class SearchHit:
    chunk_id: str
    file_path: str
    heading_path: str
    content: str
    token_count: int
    distance: float


def _serialize_embedding(vec: Sequence[float]) -> bytes:
    """sqlite-vec stores float vectors as little-endian packed float32."""
    return struct.pack(f"<{len(vec)}f", *vec)


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the index DB, load sqlite-vec, and ensure the base schema exists."""
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

        CREATE TABLE IF NOT EXISTS files (
            path        TEXT PRIMARY KEY,
            mtime       REAL NOT NULL,
            file_hash   TEXT NOT NULL,
            indexed_at  REAL NOT NULL,
            chunk_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id           TEXT PRIMARY KEY,
            file_path    TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
            ord          INTEGER NOT NULL,
            heading_path TEXT NOT NULL DEFAULT '',
            content      TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            token_count  INTEGER NOT NULL,
            byte_offset  INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path);
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


def init_for_model(conn: sqlite3.Connection, embed_model: str, embed_dims: int) -> bool:
    """Ensure the vec_chunks virtual table exists and matches the embedding model.

    Returns True if a (re)build occurred; False if it was already in sync.

    If the configured embedding model or dimensionality changes, we drop and
    recreate vec_chunks AND wipe `chunks` + `files` so the next `murano index`
    re-embeds everything. This keeps the DB internally consistent.
    """
    set_meta(conn, "schema_version", SCHEMA_VERSION)
    prior_model = get_meta(conn, "embed_model")
    prior_dims = get_meta(conn, "embed_dims")
    needs_rebuild = (
        prior_model != embed_model
        or prior_dims != str(embed_dims)
        or not _vec_table_exists(conn)
    )

    if not needs_rebuild:
        return False

    if _vec_table_exists(conn):
        conn.execute("DROP TABLE vec_chunks")
    conn.execute(
        f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding FLOAT[{embed_dims}])"
    )
    if prior_dims is not None and prior_dims != str(embed_dims):
        # Dimensions changed — existing chunks are no longer addressable.
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM files")
    set_meta(conn, "embed_model", embed_model)
    set_meta(conn, "embed_dims", str(embed_dims))
    conn.commit()
    return True


def _vec_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name = 'vec_chunks'"
    ).fetchone()
    return row is not None


def get_file_record(conn: sqlite3.Connection, file_path: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT path, mtime, file_hash, indexed_at, chunk_count FROM files WHERE path = ?",
        (file_path,),
    ).fetchone()
    return dict(row) if row else None


def list_file_paths(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT path FROM files ORDER BY path").fetchall()
    return [r["path"] for r in rows]


def delete_file(conn: sqlite3.Connection, file_path: str) -> int:
    """Remove a file and all its chunks (incl. embeddings). Returns chunks removed."""
    cur = conn.execute(
        "SELECT rowid FROM chunks WHERE file_path = ?", (file_path,)
    ).fetchall()
    rowids = [r["rowid"] for r in cur]
    for rid in rowids:
        conn.execute("DELETE FROM vec_chunks WHERE rowid = ?", (rid,))
    conn.execute("DELETE FROM chunks WHERE file_path = ?", (file_path,))
    conn.execute("DELETE FROM files WHERE path = ?", (file_path,))
    conn.commit()
    return len(rowids)


def upsert_file_with_chunks(
    conn: sqlite3.Connection,
    *,
    file_path: str,
    mtime: float,
    file_hash: str,
    indexed_at: float,
    chunks: Iterable[ChunkRow],
) -> int:
    """Replace a file's row and all its chunks atomically. Returns chunks inserted."""
    chunk_list = list(chunks)
    try:
        conn.execute("BEGIN")
        existing = conn.execute(
            "SELECT rowid FROM chunks WHERE file_path = ?", (file_path,)
        ).fetchall()
        for r in existing:
            conn.execute("DELETE FROM vec_chunks WHERE rowid = ?", (r["rowid"],))
        conn.execute("DELETE FROM chunks WHERE file_path = ?", (file_path,))

        conn.execute(
            """
            INSERT INTO files(path, mtime, file_hash, indexed_at, chunk_count)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                mtime = excluded.mtime,
                file_hash = excluded.file_hash,
                indexed_at = excluded.indexed_at,
                chunk_count = excluded.chunk_count
            """,
            (file_path, mtime, file_hash, indexed_at, len(chunk_list)),
        )

        for c in chunk_list:
            cur = conn.execute(
                """
                INSERT INTO chunks(id, file_path, ord, heading_path, content, content_hash, token_count, byte_offset)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    c.id,
                    c.file_path,
                    c.ord,
                    c.heading_path,
                    c.content,
                    c.content_hash,
                    c.token_count,
                    c.byte_offset,
                ),
            )
            conn.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
                (cur.lastrowid, _serialize_embedding(c.embedding)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(chunk_list)


def search(
    conn: sqlite3.Connection,
    query_embedding: Sequence[float],
    k: int = 10,
) -> list[SearchHit]:
    """KNN over vec_chunks, returning chunk content for each hit."""
    rows = conn.execute(
        """
        SELECT
            c.id           AS chunk_id,
            c.file_path    AS file_path,
            c.heading_path AS heading_path,
            c.content      AS content,
            c.token_count  AS token_count,
            v.distance     AS distance
        FROM vec_chunks v
        JOIN chunks c ON c.rowid = v.rowid
        WHERE v.embedding MATCH ?
          AND k = ?
        ORDER BY v.distance
        """,
        (_serialize_embedding(query_embedding), k),
    ).fetchall()
    return [
        SearchHit(
            chunk_id=r["chunk_id"],
            file_path=r["file_path"],
            heading_path=r["heading_path"],
            content=r["content"],
            token_count=r["token_count"],
            distance=r["distance"],
        )
        for r in rows
    ]


def chunk_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]


def file_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
