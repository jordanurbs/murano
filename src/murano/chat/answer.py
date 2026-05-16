"""Flat RAG answer pipeline (Phase 3).

`stream_answer` is the canonical entrypoint — it yields a stream of events that
the CLI, MCP server, and HTTP API can each render differently. The pipeline:

  1. retrieve top-k chunks via Retriever
  2. assemble a system + user prompt with the chunks as numbered context blocks
  3. open a streaming chat completion against Venice
  4. yield "retrieval" event, then "delta" events per token chunk, then "done"

Citation guarantee: the model is instructed to cite inline as
`[[file#heading]]`. To guarantee Phase 3's "at least one citation" acceptance
criterion regardless of model behaviour, callers (e.g. the CLI) always append
a "Sources" footer derived from the retrieved chunks.

The whole thing is sync so it works inside the existing typer CLI without an
event loop. We can wrap it for FastAPI's streaming response in Phase 6.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

from ..config import Settings
from .retriever import RetrievalResult, RetrievedChunk, RetrievedSummary, Retriever

DEFAULT_K = 6
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.2

EventKind = Literal["retrieval", "delta", "done", "error"]


@dataclass
class AnswerEvent:
    """One step in the streaming RAG pipeline.

    - kind="retrieval" : retrieval is `RetrievalResult` (hits, models, timing)
    - kind="delta"     : text is the next token-chunk to append to the answer
    - kind="done"      : text is the full final answer, finish_reason is set
    - kind="error"     : text is the error message
    """

    kind: EventKind
    text: str | None = None
    retrieval: RetrievalResult | None = None
    finish_reason: str | None = None


SYSTEM_PROMPT = """You are Murano, a personal knowledge-base assistant.

Answer the user's question using ONLY the provided context. If the answer is not present in the context, say so plainly — do not invent facts.

The context may include a "Themes" section: short LLM-written summaries of clusters of related notes in the vault. Themes give you orientation — use them to choose which excerpts matter and to phrase your answer with the right framing — but DO NOT cite themes. Themes are context, not sources.

The context also includes "Excerpts": numbered passages from the vault. THESE are your sources. Cite every factual claim with an Obsidian-style citation immediately after the claim, in the form `[[file#heading]]`. Use the exact citation key shown after each excerpt's `CITE:` marker. Do not invent citation keys. Multiple sources for the same claim should each be cited in their own `[[...]]` pair.

Be concise and direct. Prefer the user's terminology."""


def build_user_prompt(
    query: str,
    hits: list[RetrievedChunk],
    summaries: list[RetrievedSummary] | None = None,
) -> str:
    """Assemble the user-side prompt: question + optional themes + numbered excerpts."""
    summaries = summaries or []
    sections: list[str] = [f"Question: {query}", ""]

    if summaries:
        sections.append("Themes (background only — do NOT cite):")
        sections.append("")
        for i, s in enumerate(summaries, start=1):
            sections.append(f"({chr(96 + i)}) {s.title} [{s.member_count} notes]")
            sections.append(_indent(s.summary, prefix="     "))
            sections.append("")
        sections.append("---")
        sections.append("")

    if not hits:
        sections.append("Excerpts: (none — the knowledge base returned no matches)")
        sections.append("")
        sections.append(
            "Answer the question now. If you cannot answer from context, say so."
        )
        return "\n".join(sections)

    sections.append("Excerpts (ranked by relevance, most relevant first):")
    sections.append("")

    blocks: list[str] = []
    for i, h in enumerate(hits, start=1):
        heading_line = h.heading_path or "(no heading)"
        blocks.append(
            f"[{i}] SOURCE: {h.file_path}\n"
            f"    HEADING: {heading_line}\n"
            f"    CITE: [[{h.citation_key}]]\n"
            f"    EXCERPT:\n{_indent(h.content)}"
        )
    sections.append("\n\n---\n\n".join(blocks))
    sections.append("")
    sections.append("Answer the question now, citing each claim with the matching `[[...]]` key.")
    return "\n".join(sections)


def _indent(text: str, prefix: str = "        ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


_CITATION_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


def extract_citation_keys(text: str) -> list[str]:
    """Return the ordered, de-duplicated set of `[[...]]` keys found in text."""
    seen: list[str] = []
    for m in _CITATION_RE.finditer(text):
        key = m.group(1).strip()
        if key and key not in seen:
            seen.append(key)
    return seen


@dataclass
class StreamConfig:
    k: int = DEFAULT_K
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    include_summaries: bool = True
    summary_k: int = 2
    summary_level: int = 1
    extra: dict = field(default_factory=dict)


def stream_answer(
    settings: Settings,
    query: str,
    *,
    config: StreamConfig | None = None,
) -> Iterator[AnswerEvent]:
    """Stream a RAG answer for `query`. Yields AnswerEvent objects.

    The retriever connection is closed when the generator exits (success,
    exception, or caller stops iterating).
    """
    cfg = config or StreamConfig()
    with Retriever.open(settings) as r:
        retrieval = r.retrieve(
            query,
            k=cfg.k,
            include_summaries=cfg.include_summaries,
            summary_k=cfg.summary_k,
            summary_level=cfg.summary_level,
        )
        yield AnswerEvent(kind="retrieval", retrieval=retrieval)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_user_prompt(query, retrieval.hits, retrieval.summaries),
            },
        ]

        accumulated: list[str] = []
        finish_reason: str | None = None

        try:
            stream = r.client.chat.completions.create(
                model=retrieval.chat_model,
                messages=messages,
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
                stream=True,
                **cfg.extra,
            )
        except Exception as e:
            yield AnswerEvent(kind="error", text=f"Venice chat call failed: {e}")
            return

        try:
            for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = getattr(choice, "delta", None)
                if delta is not None:
                    piece = getattr(delta, "content", None)
                    if piece:
                        accumulated.append(piece)
                        yield AnswerEvent(kind="delta", text=piece)
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
        except Exception as e:
            yield AnswerEvent(kind="error", text=f"Stream interrupted: {e}")
            return

        yield AnswerEvent(
            kind="done",
            text="".join(accumulated),
            finish_reason=finish_reason,
            retrieval=retrieval,
        )


def collect_answer(settings: Settings, query: str, *, config: StreamConfig | None = None) -> tuple[str, RetrievalResult]:
    """Convenience: drain stream_answer into (text, retrieval). Used by MCP/HTTP later."""
    text_parts: list[str] = []
    retrieval: RetrievalResult | None = None
    for ev in stream_answer(settings, query, config=config):
        if ev.kind == "retrieval":
            retrieval = ev.retrieval
        elif ev.kind == "delta" and ev.text:
            text_parts.append(ev.text)
        elif ev.kind == "error":
            raise RuntimeError(ev.text or "unknown stream error")
    if retrieval is None:
        raise RuntimeError("stream_answer yielded no retrieval event")
    return "".join(text_parts), retrieval
