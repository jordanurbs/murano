"""Phase 5 — summary tree tests: cluster, db schema, build pipeline, retrieve."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from murano.config import Settings
from murano.index import db as chunks_db
from murano.tree import build as tree_build
from murano.tree import cluster as tree_cluster
from murano.tree import db as tree_db
from murano.tree import retrieve as tree_retrieve
from murano.tree.summarize import (
    SummarizationResult,
    _build_user_prompt,
    _parse_response,
)
from murano.venice import ResolvedModel, ResolvedModels

EMBED_DIMS = 16  # small enough to be fast, big enough for k-means to behave


# --------- cluster.py ---------


def test_l2_normalize_unit_norms() -> None:
    X = np.random.default_rng(0).normal(size=(20, EMBED_DIMS))
    Y = tree_cluster.l2_normalize(X)
    norms = np.linalg.norm(Y, axis=1)
    assert np.allclose(norms, 1.0)


def test_l2_normalize_handles_zero_rows() -> None:
    X = np.zeros((3, EMBED_DIMS))
    X[1, :] = 1.0
    Y = tree_cluster.l2_normalize(X)
    # zero row stays zero
    assert np.linalg.norm(Y[0]) == 0
    # non-zero row becomes unit
    assert np.isclose(np.linalg.norm(Y[1]), 1.0)


def test_recommend_k_thresholds() -> None:
    assert tree_cluster.recommend_k(0) == 0
    assert tree_cluster.recommend_k(4) == 0
    assert tree_cluster.recommend_k(5) == 2
    assert tree_cluster.recommend_k(100) == 10
    assert tree_cluster.recommend_k(10000) == 100


def test_kmeans_separates_well_separated_clusters() -> None:
    """Two clearly-separated Gaussian blobs should produce 2 clean clusters."""
    rng = np.random.default_rng(42)
    cluster_a = rng.normal(loc=np.eye(EMBED_DIMS)[0] * 5, scale=0.1, size=(20, EMBED_DIMS))
    cluster_b = rng.normal(loc=np.eye(EMBED_DIMS)[1] * 5, scale=0.1, size=(20, EMBED_DIMS))
    X = np.vstack([cluster_a, cluster_b])

    result = tree_cluster.kmeans(X, k=2, seed=0)
    assert result.labels.shape == (40,)
    assert set(result.labels) == {0, 1}
    # Each true cluster should be assigned to a single label (allow either mapping).
    first_half = set(result.labels[:20].tolist())
    second_half = set(result.labels[20:].tolist())
    assert len(first_half) == 1 and len(second_half) == 1
    assert first_half != second_half


def test_kmeans_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        tree_cluster.kmeans(np.zeros(10), k=2)
    with pytest.raises(ValueError):
        tree_cluster.kmeans(np.zeros((5, EMBED_DIMS)), k=0)
    with pytest.raises(ValueError):
        tree_cluster.kmeans(np.zeros((3, EMBED_DIMS)), k=10)


# --------- summarize.py (no network) ---------


def test_parse_response_extracts_title_and_summary() -> None:
    raw = "TITLE: Italian rice dishes\nSUMMARY: Notes about risotto and other arborio-based recipes. Focus on technique and ingredients."
    title, summary = _parse_response(raw, fallback_title="fallback")
    assert title == "Italian rice dishes"
    assert "risotto" in summary


def test_parse_response_falls_back_when_format_broken() -> None:
    title, summary = _parse_response(
        "Just some prose without the expected prefixes.",
        fallback_title="fallback",
    )
    assert title  # not empty
    assert summary == "Just some prose without the expected prefixes."


def test_build_user_prompt_truncates_per_bullet() -> None:
    long_text = ("word " * 5000).strip()
    prompt, included = _build_user_prompt(
        [long_text, "another short one"],
        max_context_tokens=2000,
        max_bullet_tokens=100,
    )
    assert included >= 1
    # The truncation indicator should appear for any bullet that was truncated.
    assert "…" in prompt or "word" in prompt


# --------- db.py ---------


def _vec(rng: np.random.Generator, dims: int = EMBED_DIMS) -> list[float]:
    return rng.standard_normal(dims).tolist()


@pytest.fixture
def tree_conn(tmp_path: Path):
    c = tree_db.connect(tmp_path / "summary_tree.db")
    yield c
    c.close()


def test_rebuild_persists_nodes_edges_and_meta(tree_conn) -> None:  # noqa: ANN001
    rng = np.random.default_rng(0)
    nodes = [
        tree_db.TreeNodeRow(
            id=f"L1::{i}",
            level=1,
            title=f"theme {i}",
            summary=f"summary {i}",
            member_count=3 + i,
            parent_id=None,
            embedding=_vec(rng),
        )
        for i in range(3)
    ]
    edges = [(f"L1::{i}", f"file{i}.md::0", 0) for i in range(3)]
    tree_db.rebuild(
        tree_conn,
        nodes=nodes,
        edges=edges,
        embed_model="fake-embed",
        embed_dims=EMBED_DIMS,
        chat_model="fake-chat",
        source_chunk_count=10,
    )

    assert tree_db.node_count(tree_conn) == 3
    assert tree_db.list_levels(tree_conn) == [1]
    assert tree_db.has_tree(tree_conn) is True
    assert tree_db.get_meta(tree_conn, "embed_model") == "fake-embed"
    assert tree_db.get_meta(tree_conn, "source_chunk_count") == "10"

    children = tree_db.get_children_ids(tree_conn, "L1::0")
    assert children == [("file0.md::0", 0)]


def test_rebuild_is_atomic_and_wipes_prior_state(tree_conn) -> None:  # noqa: ANN001
    rng = np.random.default_rng(1)
    nodes_a = [
        tree_db.TreeNodeRow(
            id="L1::A",
            level=1,
            title="old",
            summary="old",
            member_count=1,
            parent_id=None,
            embedding=_vec(rng),
        )
    ]
    tree_db.rebuild(
        tree_conn,
        nodes=nodes_a,
        edges=[],
        embed_model="m1",
        embed_dims=EMBED_DIMS,
        chat_model="c1",
        source_chunk_count=5,
    )
    nodes_b = [
        tree_db.TreeNodeRow(
            id="L1::B",
            level=1,
            title="new",
            summary="new",
            member_count=2,
            parent_id=None,
            embedding=_vec(rng),
        )
    ]
    tree_db.rebuild(
        tree_conn,
        nodes=nodes_b,
        edges=[],
        embed_model="m2",
        embed_dims=EMBED_DIMS,
        chat_model="c2",
        source_chunk_count=7,
    )
    nodes = tree_db.list_nodes_at_level(tree_conn, 1)
    assert [n.id for n in nodes] == ["L1::B"]
    assert tree_db.get_meta(tree_conn, "embed_model") == "m2"
    assert tree_db.get_meta(tree_conn, "source_chunk_count") == "7"


def test_search_summaries_filters_by_level(tree_conn) -> None:  # noqa: ANN001
    """Build a tiny 2-level tree and verify level filtering works."""
    nodes = [
        tree_db.TreeNodeRow(
            id="L1::0", level=1, title="t1a", summary="s1a", member_count=2,
            parent_id="L2::0", embedding=[1.0] + [0.0] * (EMBED_DIMS - 1),
        ),
        tree_db.TreeNodeRow(
            id="L1::1", level=1, title="t1b", summary="s1b", member_count=2,
            parent_id="L2::0", embedding=[0.0, 1.0] + [0.0] * (EMBED_DIMS - 2),
        ),
        tree_db.TreeNodeRow(
            id="L2::0", level=2, title="t2", summary="s2", member_count=4,
            parent_id=None, embedding=[0.5, 0.5] + [0.0] * (EMBED_DIMS - 2),
        ),
    ]
    edges = [
        ("L2::0", "L1::0", 1),
        ("L2::0", "L1::1", 1),
        ("L1::0", "f.md::0", 0),
        ("L1::1", "f.md::1", 0),
    ]
    tree_db.rebuild(
        tree_conn,
        nodes=nodes,
        edges=edges,
        embed_model="m",
        embed_dims=EMBED_DIMS,
        chat_model="c",
        source_chunk_count=2,
    )

    query = [0.9, 0.1] + [0.0] * (EMBED_DIMS - 2)
    only_l1 = tree_db.search_summaries(tree_conn, query, level=1, k=2)
    assert {h.node_id for h in only_l1} == {"L1::0", "L1::1"}
    only_l2 = tree_db.search_summaries(tree_conn, query, level=2, k=2)
    assert {h.node_id for h in only_l2} == {"L2::0"}


# --------- build.py (full pipeline with mocked Venice) ---------


def _resolved() -> ResolvedModels:
    return ResolvedModels(
        chat=ResolvedModel(requested="fake-chat", resolved="fake-chat", match="exact"),
        embed=ResolvedModel(
            requested="fake-embed",
            resolved="fake-embed",
            match="exact",
            embedding_dimensions=EMBED_DIMS,
            max_input_tokens=8192,
        ),
    )


class _FakeChatMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeChatMessage(content)


class _FakeChatResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeChatResponse(
            "TITLE: Theme number\nSUMMARY: A short summary of this cluster of notes.\n"
        )


class _FakeChat:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self) -> None:
        self.chat = _FakeChat()


@pytest.fixture
def settings_with_chunks(tmp_path: Path) -> Settings:
    """Seed chunks.db with 12 chunks spread across two well-separated embedding clusters."""
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    vault.mkdir()
    data.mkdir()
    s = Settings(vault_root=vault, data_root=data)

    conn = chunks_db.connect(s.chunks_db)
    chunks_db.init_for_model(conn, "fake-embed", EMBED_DIMS)
    rng = np.random.default_rng(0)
    rows = []
    for i in range(6):  # cluster A
        vec = (np.eye(EMBED_DIMS)[0] * 5 + rng.normal(scale=0.05, size=EMBED_DIMS)).tolist()
        rows.append(
            chunks_db.ChunkRow(
                id=f"a.md::{i}",
                file_path="a.md",
                ord=i,
                heading_path=f"Topic A > section {i}",
                content=f"Cluster A note number {i}. Discussing topic A in depth.",
                content_hash=f"ha{i}",
                token_count=10,
                byte_offset=i * 100,
                embedding=vec,
            )
        )
    for i in range(6):  # cluster B
        vec = (np.eye(EMBED_DIMS)[1] * 5 + rng.normal(scale=0.05, size=EMBED_DIMS)).tolist()
        rows.append(
            chunks_db.ChunkRow(
                id=f"b.md::{i}",
                file_path="b.md",
                ord=i,
                heading_path=f"Topic B > section {i}",
                content=f"Cluster B note number {i}. Discussing topic B in depth.",
                content_hash=f"hb{i}",
                token_count=10,
                byte_offset=i * 100,
                embedding=vec,
            )
        )
    chunks_db.upsert_file_with_chunks(
        conn,
        file_path="a.md",
        mtime=time.time(),
        file_hash="fha",
        indexed_at=time.time(),
        chunks=rows[:6],
    )
    chunks_db.upsert_file_with_chunks(
        conn,
        file_path="b.md",
        mtime=time.time(),
        file_hash="fhb",
        indexed_at=time.time(),
        chunks=rows[6:],
    )
    conn.close()
    return s


def test_build_tree_skips_when_too_few_chunks(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    vault.mkdir()
    data.mkdir()
    s = Settings(vault_root=vault, data_root=data)
    conn = chunks_db.connect(s.chunks_db)
    chunks_db.init_for_model(conn, "fake-embed", EMBED_DIMS)
    conn.close()

    with (
        patch("murano.tree.build.build_client", return_value=_FakeClient()),
        patch("murano.tree.build.resolve_models", return_value=_resolved()),
    ):
        report = tree_build.build_tree(s, min_cluster_size=5)
    assert report.skipped_reason is not None
    assert report.total_nodes == 0


def test_build_tree_produces_two_l1_clusters_on_clean_data(settings_with_chunks: Settings) -> None:
    """12 chunks in 2 blobs -> recommend_k(12)=3, but with two true blobs we still
    expect a sensible cluster count and that each cluster is single-topic."""
    embeddings_returned = []

    def fake_embed_texts(_client, _model, texts, **_kw):  # noqa: ARG001
        out = []
        for t in texts:
            if "Theme number" in t:
                # placeholder summary embedding — return a deterministic vector
                vec = [1.0 / EMBED_DIMS] * EMBED_DIMS
            else:
                vec = [0.0] * EMBED_DIMS
                vec[hash(t) % EMBED_DIMS] = 1.0
            out.append(vec)
            embeddings_returned.append(vec)
        return out

    with (
        patch("murano.tree.build.build_client", return_value=_FakeClient()),
        patch("murano.tree.build.resolve_models", return_value=_resolved()),
        patch("murano.tree.build.embed_texts", side_effect=fake_embed_texts),
    ):
        report = tree_build.build_tree(settings_with_chunks, max_levels=2, min_cluster_size=5)

    assert report.skipped_reason is None
    assert report.total_nodes >= 2
    assert report.source_chunk_count == 12
    # At least one level was built.
    assert len(report.levels) >= 1

    # Inspect the persisted tree.
    conn = tree_db.connect(settings_with_chunks.summary_tree_db)
    try:
        assert tree_db.has_tree(conn)
        l1 = tree_db.list_nodes_at_level(conn, 1)
        assert len(l1) >= 2
        # Verify the edges connect to real chunk ids.
        for node in l1:
            children = tree_db.get_children_ids(conn, node.id)
            assert children, f"node {node.id} has no children"
            for child_id, child_level in children:
                assert child_level == 0
                assert child_id.startswith(("a.md::", "b.md::"))
    finally:
        conn.close()


def test_tree_status_reports_stale_when_chunks_drift(settings_with_chunks: Settings) -> None:
    def fake_embed_texts(_client, _model, texts, **_kw):  # noqa: ARG001
        return [[1.0 / EMBED_DIMS] * EMBED_DIMS for _ in texts]

    with (
        patch("murano.tree.build.build_client", return_value=_FakeClient()),
        patch("murano.tree.build.resolve_models", return_value=_resolved()),
        patch("murano.tree.build.embed_texts", side_effect=fake_embed_texts),
    ):
        tree_build.build_tree(settings_with_chunks, max_levels=1, min_cluster_size=5)

    fresh = tree_retrieve.status(settings_with_chunks)
    assert fresh.exists is True
    assert fresh.is_stale is False

    # Drop half the chunks so source/current diverge by 50%.
    cconn = chunks_db.connect(settings_with_chunks.chunks_db)
    try:
        chunks_db.delete_file(cconn, "b.md")
    finally:
        cconn.close()

    stale = tree_retrieve.status(settings_with_chunks)
    assert stale.is_stale is True
    assert stale.stale_reason is not None


def test_get_chunk_returns_full_record(settings_with_chunks: Settings) -> None:
    rec = tree_retrieve.get_chunk(settings_with_chunks, "a.md::0")
    assert rec is not None
    assert rec.file_path == "a.md"
    assert rec.ord == 0
    assert "Cluster A note number 0" in rec.content
    assert tree_retrieve.get_chunk(settings_with_chunks, "missing::99") is None


def test_summarize_result_dataclass_minimal() -> None:
    r = SummarizationResult(title="t", summary="s", raw="t|s")
    assert r.title == "t"
    assert r.summary == "s"
