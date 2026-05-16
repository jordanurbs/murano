"""Embedding helper: batches texts through Venice's embeddings endpoint."""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

from openai import OpenAI

from ..usage import UsageEvent, extract_usage_from_response, log_usage

DEFAULT_BATCH_SIZE = 32


def embed_texts(
    client: OpenAI,
    model: str,
    texts: Sequence[str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    usage_log_dir: Path | None = None,
    operation: str = "embed",
) -> list[list[float]]:
    """Embed `texts` via Venice, preserving input order.

    Returns one float vector per input. Raises whatever the OpenAI SDK raises
    on transport/HTTP errors so callers can decide retry policy.

    If `usage_log_dir` is supplied, a UsageEvent is appended per batch
    (silently swallows logging failures).
    """
    if not texts:
        return []
    out: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = list(texts[start : start + batch_size])
        t0 = time.monotonic()
        resp = client.embeddings.create(model=model, input=batch)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        ordered = sorted(resp.data, key=lambda d: d.index)
        out.extend(d.embedding for d in ordered)
        if usage_log_dir is not None:
            p, c, t = extract_usage_from_response(resp)
            log_usage(
                usage_log_dir,
                UsageEvent(
                    operation=operation,
                    model=model,
                    prompt_tokens=p,
                    completion_tokens=c,
                    total_tokens=t,
                    elapsed_ms=elapsed_ms,
                    extra={"batch_size": len(batch)},
                ),
            )
    return out


def embed_one(
    client: OpenAI,
    model: str,
    text: str,
    *,
    usage_log_dir: Path | None = None,
    operation: str = "embed",
) -> list[float]:
    """Embed a single text — convenience wrapper for query embedding."""
    return embed_texts(
        client,
        model,
        [text],
        usage_log_dir=usage_log_dir,
        operation=operation,
    )[0]
