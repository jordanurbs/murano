"""Embedding helper: batches texts through Venice's embeddings endpoint."""

from __future__ import annotations

from collections.abc import Sequence

from openai import OpenAI

DEFAULT_BATCH_SIZE = 32


def embed_texts(
    client: OpenAI,
    model: str,
    texts: Sequence[str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[list[float]]:
    """Embed `texts` via Venice, preserving input order.

    Returns one float vector per input. Raises whatever the OpenAI SDK raises
    on transport/HTTP errors so callers can decide retry policy.
    """
    if not texts:
        return []
    out: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = list(texts[start : start + batch_size])
        resp = client.embeddings.create(model=model, input=batch)
        ordered = sorted(resp.data, key=lambda d: d.index)
        out.extend(d.embedding for d in ordered)
    return out


def embed_one(client: OpenAI, model: str, text: str) -> list[float]:
    """Embed a single text — convenience wrapper for query embedding."""
    return embed_texts(client, model, [text])[0]
