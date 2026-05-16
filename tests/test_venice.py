"""Phase 1 — Venice model resolution tests (network is fully mocked)."""

from __future__ import annotations

from unittest.mock import patch

from murano.config import Settings
from murano.venice import _best_match, resolve_models


def test_best_match_prefers_exact() -> None:
    resolved, kind = _best_match("qwen-3-6-plus", ["qwen-3-6-plus", "qwen-3-6"])
    assert resolved == "qwen-3-6-plus"
    assert kind == "exact"


def test_best_match_falls_back_to_prefix() -> None:
    resolved, kind = _best_match(
        "text-embedding-qwen3-8b",
        ["text-embedding-qwen3-8b-v2", "other"],
    )
    assert resolved == "text-embedding-qwen3-8b-v2"
    assert kind == "prefix"


def test_best_match_passes_through_when_no_candidate() -> None:
    resolved, kind = _best_match("missing-model", ["a", "b", "c"])
    assert resolved == "missing-model"
    assert kind == "none"


def test_resolve_models_against_fake_venice() -> None:
    settings = Settings(
        chat_model="qwen-3-6-plus",
        embed_model="text-embedding-qwen3-8b",
    )
    text_models = [
        {"id": "qwen-3-6-plus"},
        {"id": "claude-opus-4-7"},
    ]
    embed_models = [
        {
            "id": "text-embedding-qwen3-8b",
            "model_spec": {"embeddingDimensions": 4096, "maxInputTokens": 32768},
        },
        {"id": "text-embedding-bge-m3", "model_spec": {"embeddingDimensions": 1024}},
    ]

    def fake_get(_settings, type_filter):  # noqa: ANN001
        if type_filter == "text":
            return text_models
        if type_filter == "embedding":
            return embed_models
        return text_models + embed_models

    with patch("murano.venice._http_get_models", side_effect=fake_get):
        resolved = resolve_models(settings)

    assert resolved.chat.resolved == "qwen-3-6-plus"
    assert resolved.chat.match == "exact"
    assert resolved.embed.resolved == "text-embedding-qwen3-8b"
    assert resolved.embed.match == "exact"
    assert resolved.embed.embedding_dimensions == 4096
    assert resolved.embed.max_input_tokens == 32768


def test_resolve_models_warns_when_embed_missing() -> None:
    settings = Settings(chat_model="qwen-3-6-plus", embed_model="ghost-model")

    def fake_get(_settings, type_filter):  # noqa: ANN001
        if type_filter == "text":
            return [{"id": "qwen-3-6-plus"}]
        if type_filter == "embedding":
            return [{"id": "text-embedding-qwen3-8b", "model_spec": {}}]
        return []

    with patch("murano.venice._http_get_models", side_effect=fake_get):
        resolved = resolve_models(settings)

    assert resolved.embed.match == "none"
    assert resolved.embed.resolved == "ghost-model"
    assert resolved.embed.embedding_dimensions is None
