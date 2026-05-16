"""Phase 2 — sqlite-vec storage tests (no Venice calls; embeddings are fake floats)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from murano.index import db as dbmod

EMBED_DIMS = 8  # tiny so tests are cheap


def _vec(*xs: float) -> list[float]:
    assert len(xs) == EMBED_DIMS
    return list(xs)


@pytest.fixture
def conn(tmp_path: Path):
    c = dbmod.connect(tmp_path / "chunks.db")
    dbmod.init_for_model(c, "fake-embed", EMBED_DIMS)
    yield c
    c.close()


def _chunk(file_path: str, ord_: int, content: str, embedding: list[float]) -> dbmod.ChunkRow:
    return dbmod.ChunkRow(
        id=f"{file_path}::{ord_}",
        file_path=file_path,
        ord=ord_,
        heading_path=f"H {ord_}",
        content=content,
        content_hash=f"h{ord_}",
        token_count=len(content.split()),
        byte_offset=ord_ * 100,
        embedding=embedding,
    )


def test_round_trip_and_search_returns_nearest_first(conn) -> None:  # noqa: ANN001
    dbmod.upsert_file_with_chunks(
        conn,
        file_path="notes/a.md",
        mtime=time.time(),
        file_hash="fh-a",
        indexed_at=time.time(),
        chunks=[
            _chunk("notes/a.md", 0, "alpha bravo", _vec(1, 0, 0, 0, 0, 0, 0, 0)),
            _chunk("notes/a.md", 1, "charlie delta", _vec(0, 1, 0, 0, 0, 0, 0, 0)),
            _chunk("notes/a.md", 2, "echo foxtrot", _vec(0, 0, 1, 0, 0, 0, 0, 0)),
        ],
    )

    assert dbmod.file_count(conn) == 1
    assert dbmod.chunk_count(conn) == 3

    hits = dbmod.search(conn, _vec(0.9, 0.1, 0, 0, 0, 0, 0, 0), k=3)
    assert len(hits) == 3
    assert hits[0].chunk_id == "notes/a.md::0"
    assert hits[1].chunk_id == "notes/a.md::1"
    assert hits[2].chunk_id == "notes/a.md::2"


def test_upsert_replaces_old_chunks_atomically(conn) -> None:  # noqa: ANN001
    dbmod.upsert_file_with_chunks(
        conn,
        file_path="a.md",
        mtime=1.0,
        file_hash="h1",
        indexed_at=1.0,
        chunks=[_chunk("a.md", i, f"v1-{i}", _vec(i, 0, 0, 0, 0, 0, 0, 0)) for i in range(4)],
    )
    assert dbmod.chunk_count(conn) == 4

    dbmod.upsert_file_with_chunks(
        conn,
        file_path="a.md",
        mtime=2.0,
        file_hash="h2",
        indexed_at=2.0,
        chunks=[_chunk("a.md", i, f"v2-{i}", _vec(i, 0, 0, 0, 0, 0, 0, 0)) for i in range(2)],
    )
    assert dbmod.chunk_count(conn) == 2

    rec = dbmod.get_file_record(conn, "a.md")
    assert rec is not None
    assert rec["file_hash"] == "h2"
    assert rec["chunk_count"] == 2


def test_delete_file_removes_chunks_and_vectors(conn) -> None:  # noqa: ANN001
    dbmod.upsert_file_with_chunks(
        conn,
        file_path="a.md",
        mtime=1.0,
        file_hash="h1",
        indexed_at=1.0,
        chunks=[_chunk("a.md", i, f"c{i}", _vec(i, 0, 0, 0, 0, 0, 0, 0)) for i in range(3)],
    )
    removed = dbmod.delete_file(conn, "a.md")
    assert removed == 3
    assert dbmod.chunk_count(conn) == 0
    assert dbmod.file_count(conn) == 0

    hits = dbmod.search(conn, _vec(1, 0, 0, 0, 0, 0, 0, 0), k=5)
    assert hits == []


def test_init_for_model_with_new_dims_wipes_chunks(tmp_path: Path) -> None:
    c = dbmod.connect(tmp_path / "chunks.db")
    dbmod.init_for_model(c, "embed-a", EMBED_DIMS)
    dbmod.upsert_file_with_chunks(
        c,
        file_path="a.md",
        mtime=1.0,
        file_hash="h",
        indexed_at=1.0,
        chunks=[_chunk("a.md", 0, "content", _vec(1, 0, 0, 0, 0, 0, 0, 0))],
    )
    assert dbmod.chunk_count(c) == 1

    rebuilt = dbmod.init_for_model(c, "embed-b", 16)
    assert rebuilt is True
    assert dbmod.chunk_count(c) == 0
    assert dbmod.file_count(c) == 0

    rebuilt_again = dbmod.init_for_model(c, "embed-b", 16)
    assert rebuilt_again is False
    c.close()
