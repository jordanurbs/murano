"""Venice API client.

Murano talks to Venice through the official `openai` SDK with the base URL
swapped to `https://api.venice.ai/api/v1`. This is the ONLY outbound network
target Murano is allowed to contact.

Venice's `/v1/models` endpoint accepts a `type` query parameter
(`text` | `embedding` | `image` | `tts` | `upscale`). The OpenAI SDK doesn't
expose that parameter, so for catalog listing we go through `httpx` directly;
all actual chat/embedding requests still flow through the OpenAI client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from openai import OpenAI

from .config import Settings, get_api_key


class VeniceAuthError(RuntimeError):
    """Raised when no Venice API key is configured."""


class VeniceConnectionError(RuntimeError):
    """Raised when Venice cannot be reached or returns an error."""


@dataclass
class ResolvedModel:
    """A single resolved model along with the catalog metadata Murano cares about."""

    requested: str
    resolved: str
    match: str  # "exact" | "prefix" | "none"
    embedding_dimensions: int | None = None
    max_input_tokens: int | None = None


@dataclass
class ResolvedModels:
    """Result of resolving requested chat + embedding model IDs against Venice."""

    chat: ResolvedModel
    embed: ResolvedModel


def build_client(settings: Settings) -> OpenAI:
    """Construct an OpenAI client pointed at Venice, using the keychain API key."""
    api_key = get_api_key()
    if not api_key:
        raise VeniceAuthError(
            "No Venice API key found in the OS keychain. "
            "Run `murano config set-key` to store one."
        )
    return OpenAI(api_key=api_key, base_url=settings.venice_base_url)


def _http_get_models(settings: Settings, type_filter: str | None) -> list[dict[str, Any]]:
    """Call Venice's `/v1/models` directly so we can pass `?type=<filter>`.

    Returns the raw `data` array (list of model dicts) so callers can read
    Venice-specific fields like `model_spec.embeddingDimensions`.
    """
    api_key = get_api_key()
    if not api_key:
        raise VeniceAuthError(
            "No Venice API key found in the OS keychain. "
            "Run `murano config set-key` to store one."
        )
    params = {"type": type_filter} if type_filter else {}
    try:
        resp = httpx.get(
            f"{settings.venice_base_url.rstrip('/')}/models",
            params=params,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise VeniceConnectionError(f"Failed to reach Venice /v1/models: {e}") from e
    payload = resp.json()
    return list(payload.get("data", []))


def list_text_model_ids(settings: Settings) -> list[str]:
    """Return all text/chat model IDs Venice advertises."""
    return [m["id"] for m in _http_get_models(settings, "text") if "id" in m]


def list_embedding_models(settings: Settings) -> list[dict[str, Any]]:
    """Return the full embedding model records (id + model_spec)."""
    return _http_get_models(settings, "embedding")


def list_all_model_ids(settings: Settings) -> list[str]:
    """Return every model ID across every type (used by `murano models`)."""
    return [m["id"] for m in _http_get_models(settings, None) if "id" in m]


def _best_match(requested: str, available: list[str]) -> tuple[str, str]:
    """Resolve `requested` against `available`.

    Returns `(resolved_id, match_kind)` where `match_kind` is:
      - "exact"  → `requested` appears verbatim in `available`
      - "prefix" → first available ID that starts with `requested`
      - "none"   → no candidate found; `resolved_id == requested` (pass-through)
    """
    if requested in available:
        return requested, "exact"
    prefix_matches = [m for m in available if m.startswith(requested)]
    if prefix_matches:
        return prefix_matches[0], "prefix"
    return requested, "none"


def resolve_models(settings: Settings) -> ResolvedModels:
    """Resolve the configured chat + embed model IDs against Venice's typed catalogs."""
    text_ids = list_text_model_ids(settings)
    embed_records = list_embedding_models(settings)
    embed_ids = [m["id"] for m in embed_records]

    chat_id, chat_match = _best_match(settings.chat_model, text_ids)
    embed_id, embed_match = _best_match(settings.embed_model, embed_ids)

    chat = ResolvedModel(
        requested=settings.chat_model,
        resolved=chat_id,
        match=chat_match,
    )

    embed = ResolvedModel(
        requested=settings.embed_model,
        resolved=embed_id,
        match=embed_match,
    )
    for record in embed_records:
        if record.get("id") == embed_id:
            spec = record.get("model_spec", {}) or {}
            dims = spec.get("embeddingDimensions")
            max_tok = spec.get("maxInputTokens")
            if isinstance(dims, int):
                embed.embedding_dimensions = dims
            if isinstance(max_tok, int):
                embed.max_input_tokens = max_tok
            break

    return ResolvedModels(chat=chat, embed=embed)
