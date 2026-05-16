"""Phase 2 — vault indexer integration tests (Venice fully mocked)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from murano.config import Settings
from murano.index import db as dbmod
from murano.index.indexer import index_vault, reindex_vault
from murano.venice import ResolvedModel, ResolvedModels

EMBED_DIMS = 8


def _resolved(embed_model: str = "fake-embed", dims: int = EMBED_DIMS) -> ResolvedModels:
    return ResolvedModels(
        chat=ResolvedModel(requested="qwen-3-6-plus", resolved="qwen-3-6-plus", match="exact"),
        embed=ResolvedModel(
            requested=embed_model,
            resolved=embed_model,
            match="exact",
            embedding_dimensions=dims,
            max_input_tokens=8192,
        ),
    )


def _fake_embed_texts(_client, _model, texts, *, batch_size: int = 32):  # noqa: ARG001
    out = []
    for t in texts:
        vec = [0.0] * EMBED_DIMS
        seed = hash(t) & 0xFFFFFFFF
        vec[seed % EMBED_DIMS] = 1.0
        out.append(vec)
    return out


class _FakeClient:
    pass


@pytest.fixture
def vault(tmp_path: Path) -> Settings:
    vault_root = tmp_path / "vault"
    data_root = tmp_path / "data"
    vault_root.mkdir()
    data_root.mkdir()
    return Settings(vault_root=vault_root, data_root=data_root)


def _write(vault_root: Path, relpath: str, body: str) -> Path:
    p = vault_root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _patches():
    return (
        patch("murano.index.indexer.build_client", return_value=_FakeClient()),
        patch("murano.index.indexer.resolve_models", return_value=_resolved()),
        patch("murano.index.indexer.embed_texts", side_effect=_fake_embed_texts),
    )


def test_index_creates_chunks_for_new_files(vault: Settings) -> None:
    _write(vault.vault_root, "a.md", "# Note A\n\nalpha bravo charlie.\n")
    _write(vault.vault_root, "sub/b.md", "# Note B\n\ndelta echo foxtrot.\n")

    p1, p2, p3 = _patches()
    with p1, p2, p3:
        report = index_vault(vault)

    assert report.files_seen == 2
    assert report.files_indexed == 2
    assert report.files_unchanged == 0
    assert report.chunks_inserted >= 2
    assert not report.errors

    conn = dbmod.connect(vault.chunks_db)
    try:
        assert dbmod.file_count(conn) == 2
        files = sorted(dbmod.list_file_paths(conn))
        assert files == ["a.md", "sub/b.md"]
    finally:
        conn.close()


def test_unchanged_file_is_skipped_on_second_run(vault: Settings) -> None:
    _write(vault.vault_root, "a.md", "# A\n\ncontent\n")

    p1, p2, p3 = _patches()
    with p1, p2, p3:
        index_vault(vault)
        report2 = index_vault(vault)

    assert report2.files_indexed == 0
    assert report2.files_unchanged == 1
    assert report2.embedding_calls == 0


def test_modified_file_is_reindexed(vault: Settings) -> None:
    _write(vault.vault_root, "a.md", "# A\n\nbefore\n")

    p1, p2, p3 = _patches()
    with p1, p2, p3:
        index_vault(vault)
        time.sleep(0.01)
        _write(vault.vault_root, "a.md", "# A\n\nafter (changed)\n")
        report2 = index_vault(vault)

    assert report2.files_indexed == 1
    assert report2.files_unchanged == 0


def test_deleted_file_is_pruned(vault: Settings) -> None:
    p_a = _write(vault.vault_root, "a.md", "# A\n\nalpha\n")
    _write(vault.vault_root, "b.md", "# B\n\nbravo\n")

    p1, p2, p3 = _patches()
    with p1, p2, p3:
        index_vault(vault)
        p_a.unlink()
        report2 = index_vault(vault)

    assert report2.files_removed == 1
    assert report2.files_seen == 1

    conn = dbmod.connect(vault.chunks_db)
    try:
        assert dbmod.list_file_paths(conn) == ["b.md"]
    finally:
        conn.close()


def test_subpath_limits_scope(vault: Settings) -> None:
    _write(vault.vault_root, "top.md", "# Top\n\ntop content\n")
    _write(vault.vault_root, "sub/inside.md", "# Inside\n\nsub content\n")

    p1, p2, p3 = _patches()
    with p1, p2, p3:
        index_vault(vault)
        time.sleep(0.01)
        _write(vault.vault_root, "sub/inside.md", "# Inside\n\nsub CHANGED\n")
        report = index_vault(vault, subpath=Path("sub"))

    assert report.files_seen == 1
    assert report.files_indexed == 1
    assert report.files_removed == 0


def test_reindex_wipes_and_rebuilds(vault: Settings) -> None:
    _write(vault.vault_root, "a.md", "# A\n\nalpha\n")

    p1, p2, p3 = _patches()
    with p1, p2, p3:
        index_vault(vault)
        report2 = reindex_vault(vault)

    assert report2.files_indexed == 1
    assert report2.files_unchanged == 0
    assert vault.chunks_db.exists()


def test_no_api_call_when_vault_empty(vault: Settings) -> None:
    p1, p2, p3 = _patches()
    with p1, p2, p3:
        report = index_vault(vault)
    assert report.files_seen == 0
    assert report.chunks_inserted == 0
    assert report.embedding_calls == 0
