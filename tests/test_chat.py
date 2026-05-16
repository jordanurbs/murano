"""Phase 3 — RAG retriever + answer pipeline tests (Venice fully mocked)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from murano.chat.answer import (
    SYSTEM_PROMPT,
    AnswerEvent,
    build_user_prompt,
    collect_answer,
    extract_citation_keys,
    stream_answer,
)
from murano.chat.retriever import Retriever, derive_citation_key
from murano.config import Settings
from murano.index import db as dbmod
from murano.tree import db as tree_db
from murano.venice import ResolvedModel, ResolvedModels

EMBED_DIMS = 8


def _vec(*xs: float) -> list[float]:
    assert len(xs) == EMBED_DIMS
    return list(xs)


def _resolved() -> ResolvedModels:
    return ResolvedModels(
        chat=ResolvedModel(requested="qwen-3-6-plus", resolved="qwen-3-6-plus", match="exact"),
        embed=ResolvedModel(
            requested="fake-embed",
            resolved="fake-embed",
            match="exact",
            embedding_dimensions=EMBED_DIMS,
            max_input_tokens=8192,
        ),
    )


@pytest.fixture
def settings_with_index(tmp_path: Path) -> Settings:
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    vault.mkdir()
    data.mkdir()
    s = Settings(vault_root=vault, data_root=data)

    conn = dbmod.connect(s.chunks_db)
    dbmod.init_for_model(conn, "fake-embed", EMBED_DIMS)
    dbmod.upsert_file_with_chunks(
        conn,
        file_path="cooking/risotto.md",
        mtime=time.time(),
        file_hash="h1",
        indexed_at=time.time(),
        chunks=[
            dbmod.ChunkRow(
                id="cooking/risotto.md::0",
                file_path="cooking/risotto.md",
                ord=0,
                heading_path="Mushroom Risotto \u203a Method",
                content="Saute mushrooms, deglaze with wine, add stock gradually.",
                content_hash="cha",
                token_count=10,
                byte_offset=0,
                embedding=_vec(1, 0, 0, 0, 0, 0, 0, 0),
            ),
            dbmod.ChunkRow(
                id="cooking/risotto.md::1",
                file_path="cooking/risotto.md",
                ord=1,
                heading_path="Mushroom Risotto \u203a Ingredients",
                content="Arborio rice, cremini mushrooms, shallot, wine, parmesan.",
                content_hash="chb",
                token_count=9,
                byte_offset=100,
                embedding=_vec(0, 1, 0, 0, 0, 0, 0, 0),
            ),
        ],
    )
    conn.close()
    return s


def test_derive_citation_key_drops_extension_and_takes_leaf_heading() -> None:
    assert (
        derive_citation_key("cooking/risotto.md", "Mushroom Risotto \u203a Method")
        == "cooking/risotto#Method"
    )
    assert derive_citation_key("notes.markdown", "") == "notes"
    assert derive_citation_key("notes.MD", "Title \u203a Sub") == "notes#Sub"  # case-insensitive
    assert derive_citation_key("no/extension", "Heading") == "no/extension#Heading"


def test_extract_citation_keys_orders_and_dedupes() -> None:
    text = (
        "Risotto needs slow stock addition [[cooking/risotto#Method]]. "
        "Toast the rice first [[cooking/risotto#Method]]. "
        "Use arborio rice [[cooking/risotto#Ingredients]]."
    )
    assert extract_citation_keys(text) == [
        "cooking/risotto#Method",
        "cooking/risotto#Ingredients",
    ]


def test_extract_citation_keys_ignores_malformed() -> None:
    text = "no citations here. [single brackets] and [[]] empty."
    assert extract_citation_keys(text) == []


def test_build_user_prompt_includes_cite_keys_and_excerpts() -> None:
    from murano.chat.retriever import RetrievedChunk

    hits = [
        RetrievedChunk(
            chunk_id="a::0",
            file_path="a.md",
            heading_path="Foo \u203a Bar",
            content="Some content here.",
            token_count=4,
            distance=0.5,
            citation_key="a#Bar",
        )
    ]
    prompt = build_user_prompt("what is foo bar?", hits)
    assert "Question: what is foo bar?" in prompt
    assert "[[a#Bar]]" in prompt
    assert "Some content here." in prompt
    assert "SOURCE: a.md" in prompt
    assert "HEADING: Foo \u203a Bar" in prompt


def test_build_user_prompt_handles_empty_hits() -> None:
    prompt = build_user_prompt("orphan question", [])
    assert "no matches" in prompt


def test_retriever_returns_ranked_hits_with_citation_keys(settings_with_index: Settings) -> None:
    """Real Retriever, mocked Venice client + embedder."""

    class _FakeClient:
        pass

    def fake_embed_one(_client, _model, text):  # noqa: ARG001
        return _vec(0.9, 0.1, 0, 0, 0, 0, 0, 0)

    with (
        patch("murano.chat.retriever.build_client", return_value=_FakeClient()),
        patch("murano.chat.retriever.resolve_models", return_value=_resolved()),
        patch("murano.chat.retriever.embed_one", side_effect=fake_embed_one),
        Retriever.open(settings_with_index) as r,
    ):
        result = r.retrieve("how do I make risotto", k=2)

    assert result.chat_model == "qwen-3-6-plus"
    assert result.embed_model == "fake-embed"
    assert result.embed_dims == EMBED_DIMS
    assert len(result.hits) == 2
    assert result.hits[0].citation_key == "cooking/risotto#Method"
    assert result.hits[1].citation_key == "cooking/risotto#Ingredients"
    assert result.hits[0].distance < result.hits[1].distance


class _FakeDelta:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None, finish_reason: str | None = None) -> None:
        self.delta = _FakeDelta(content)
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, content: str | None, finish_reason: str | None = None) -> None:
        self.choices = [_FakeChoice(content, finish_reason)]


class _FakeCompletions:
    def __init__(self, pieces: list[str]) -> None:
        self._pieces = pieces

    def create(self, **kwargs: Any):
        assert kwargs["stream"] is True
        for p in self._pieces[:-1]:
            yield _FakeChunk(p)
        yield _FakeChunk(self._pieces[-1], finish_reason="stop")


class _FakeChat:
    def __init__(self, pieces: list[str]) -> None:
        self.completions = _FakeCompletions(pieces)


class _FakeClient:
    def __init__(self, pieces: list[str]) -> None:
        self.chat = _FakeChat(pieces)


def _patch_chat(pieces: list[str], embed_vec: list[float] | None = None):
    embed_vec = embed_vec or _vec(0.9, 0.1, 0, 0, 0, 0, 0, 0)
    return [
        patch("murano.chat.retriever.build_client", return_value=_FakeClient(pieces)),
        patch("murano.chat.retriever.resolve_models", return_value=_resolved()),
        patch("murano.chat.retriever.embed_one", return_value=embed_vec),
    ]


def test_stream_answer_yields_retrieval_then_deltas_then_done(settings_with_index: Settings) -> None:
    pieces = [
        "Risotto ",
        "needs ",
        "slow stock addition ",
        "[[cooking/risotto#Method]].",
    ]
    p1, p2, p3 = _patch_chat(pieces)
    events: list[AnswerEvent] = []
    with p1, p2, p3:
        for ev in stream_answer(settings_with_index, "how do I make risotto?"):
            events.append(ev)

    kinds = [e.kind for e in events]
    assert kinds[0] == "retrieval"
    assert kinds[-1] == "done"
    assert kinds.count("delta") == len(pieces)
    deltas = [e.text for e in events if e.kind == "delta"]
    assert "".join(deltas) == "Risotto needs slow stock addition [[cooking/risotto#Method]]."

    done = events[-1]
    assert done.text == "Risotto needs slow stock addition [[cooking/risotto#Method]]."
    assert done.finish_reason == "stop"
    assert done.retrieval is not None
    assert len(done.retrieval.hits) >= 1


def test_collect_answer_returns_text_and_retrieval(settings_with_index: Settings) -> None:
    pieces = ["Use ", "arborio rice ", "[[cooking/risotto#Ingredients]]."]
    p1, p2, p3 = _patch_chat(pieces)
    with p1, p2, p3:
        text, retrieval = collect_answer(settings_with_index, "what kind of rice?")
    assert text == "Use arborio rice [[cooking/risotto#Ingredients]]."
    assert any(h.citation_key == "cooking/risotto#Ingredients" for h in retrieval.hits)
    cited = extract_citation_keys(text)
    assert "cooking/risotto#Ingredients" in cited


def test_system_prompt_mentions_citation_format_and_themes() -> None:
    """Guardrail: the model instructions must spell out the Obsidian citation format AND
    that themes are context, not sources."""
    assert "[[file#heading]]" in SYSTEM_PROMPT
    assert "ONLY the provided context" in SYSTEM_PROMPT
    assert "DO NOT cite themes" in SYSTEM_PROMPT


def test_hybrid_retrieve_includes_summaries_when_tree_present(settings_with_index: Settings) -> None:
    """If a summary_tree.db exists, retrieve() pulls top-N summaries alongside chunks."""

    class _FakeClient:
        pass

    # Seed a tiny tree pointing at the chunks already in settings_with_index.
    tconn = tree_db.connect(settings_with_index.summary_tree_db)
    try:
        tree_db.rebuild(
            tconn,
            nodes=[
                tree_db.TreeNodeRow(
                    id="L1::method",
                    level=1,
                    title="Risotto cooking method",
                    summary="How to cook risotto: saute, deglaze, ladle.",
                    member_count=1,
                    parent_id=None,
                    embedding=_vec(1, 0, 0, 0, 0, 0, 0, 0),
                ),
                tree_db.TreeNodeRow(
                    id="L1::ingr",
                    level=1,
                    title="Risotto ingredients",
                    summary="What goes into mushroom risotto.",
                    member_count=1,
                    parent_id=None,
                    embedding=_vec(0, 1, 0, 0, 0, 0, 0, 0),
                ),
            ],
            edges=[
                ("L1::method", "cooking/risotto.md::0", 0),
                ("L1::ingr", "cooking/risotto.md::1", 0),
            ],
            embed_model="fake-embed",
            embed_dims=EMBED_DIMS,
            chat_model="qwen-3-6-plus",
            source_chunk_count=2,
        )
    finally:
        tconn.close()

    def fake_embed_one(_client, _model, text):  # noqa: ARG001
        return _vec(0.9, 0.1, 0, 0, 0, 0, 0, 0)

    with (
        patch("murano.chat.retriever.build_client", return_value=_FakeClient()),
        patch("murano.chat.retriever.resolve_models", return_value=_resolved()),
        patch("murano.chat.retriever.embed_one", side_effect=fake_embed_one),
        Retriever.open(settings_with_index) as r,
    ):
        result = r.retrieve("how do I cook risotto?", k=2, include_summaries=True, summary_k=2)

    assert result.hits  # chunks still come back
    assert len(result.summaries) == 2
    assert {s.node_id for s in result.summaries} == {"L1::method", "L1::ingr"}
    # The summaries are ordered by distance — nearest first.
    assert result.summaries[0].distance <= result.summaries[1].distance


def test_hybrid_retrieve_is_no_op_when_no_tree(settings_with_index: Settings) -> None:
    """No tree DB present -> summaries is empty, hits unchanged."""

    class _FakeClient:
        pass

    def fake_embed_one(_client, _model, text):  # noqa: ARG001
        return _vec(0.9, 0.1, 0, 0, 0, 0, 0, 0)

    assert not settings_with_index.summary_tree_db.exists()
    with (
        patch("murano.chat.retriever.build_client", return_value=_FakeClient()),
        patch("murano.chat.retriever.resolve_models", return_value=_resolved()),
        patch("murano.chat.retriever.embed_one", side_effect=fake_embed_one),
        Retriever.open(settings_with_index) as r,
    ):
        result = r.retrieve("how do I cook risotto?", k=2, include_summaries=True, summary_k=2)
    assert result.summaries == []
    assert len(result.hits) == 2
