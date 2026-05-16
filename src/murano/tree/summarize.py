"""LLM-driven cluster summarization.

For each cluster of chunks (or child summaries), we ask the Venice chat
model to produce a short title (~6 words) and a 3-5 sentence summary that
captures the theme. The model output is parsed with a tolerant regex; any
deviation falls back to a deterministic stub so a single bad LLM response
doesn't blow up the whole tree build.

We deliberately pass the chunks as plain text bullets — no Murano-specific
prompt formatting (no `[[file#heading]]` citations etc.) — because summaries
are *context*, not citations. Citations are still anchored to leaf chunks
during retrieval/answer time.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..vault.chunker import count_tokens

DEFAULT_MAX_CONTEXT_TOKENS = 6000  # cap for a single summarization request
DEFAULT_MAX_BULLET_TOKENS = 600    # truncate each member to keep the prompt diverse
DEFAULT_SUMMARY_MAX_TOKENS = 220   # how many tokens we allow the model to use
DEFAULT_TEMPERATURE = 0.2

SYSTEM_PROMPT = (
    "You write very short thematic summaries of text clusters. "
    "Given a list of text excerpts that share a theme, produce: "
    "(1) a 4-8 word TITLE capturing the theme, and "
    "(2) a 3-5 sentence SUMMARY that captures the gist of what these texts "
    "are about. "
    "Do not invent facts; just describe what the texts collectively discuss. "
    "Do not cite individual passages. Do not use markdown."
)

USER_TEMPLATE = (
    "Excerpts (one per bullet, may be truncated):\n\n"
    "{bullets}\n\n"
    "Respond in EXACTLY this format:\n"
    "TITLE: <4-8 words>\n"
    "SUMMARY: <3-5 sentences on a single line>\n"
)


@dataclass
class SummarizationResult:
    title: str
    summary: str
    raw: str  # raw model output, useful for debugging


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Drop tail of `text` so its token count fits under `max_tokens`."""
    if count_tokens(text) <= max_tokens:
        return text
    # Cheap approximation: tokens ~= 0.75 * words on average for cl100k_base.
    # Repeatedly slice & re-check until under cap.
    words = text.split()
    lo, hi = 0, len(words)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = " ".join(words[:mid])
        if count_tokens(candidate) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return " ".join(words[:lo]) + " …"


def _build_user_prompt(
    member_texts: Sequence[str],
    *,
    max_context_tokens: int,
    max_bullet_tokens: int,
) -> tuple[str, int]:
    """Assemble bullets, truncating per-bullet and per-context to stay under caps.

    Returns (prompt, members_included_count).
    """
    bullets: list[str] = []
    total = 0
    skeleton = USER_TEMPLATE.format(bullets="")
    budget = max_context_tokens - count_tokens(skeleton) - 64  # leave room for prose
    for text in member_texts:
        snippet = _truncate_to_tokens(text.strip(), max_bullet_tokens)
        bullet = f"- {snippet}"
        cost = count_tokens(bullet) + 2
        if bullets and total + cost > budget:
            break
        bullets.append(bullet)
        total += cost
    if not bullets:
        bullets.append("- (cluster contained no usable text)")
        return USER_TEMPLATE.format(bullets="\n".join(bullets)), 0
    return USER_TEMPLATE.format(bullets="\n".join(bullets)), len(bullets)


_TITLE_RE = re.compile(r"(?im)^\s*title\s*:\s*(.+?)\s*$")
_SUMMARY_RE = re.compile(r"(?ims)^\s*summary\s*:\s*(.+?)\s*$")


def _parse_response(raw: str, fallback_title: str) -> tuple[str, str]:
    """Pull TITLE / SUMMARY from the model output, with safe fallbacks."""
    title_m = _TITLE_RE.search(raw)
    summary_m = _SUMMARY_RE.search(raw)

    title = title_m.group(1).strip() if title_m else ""
    summary = summary_m.group(1).strip() if summary_m else ""

    if not summary:
        # Sometimes the model skips the SUMMARY prefix entirely.
        summary = raw.strip()
        if title and summary.lower().startswith("title:"):
            summary = summary.split("\n", 1)[-1] if "\n" in summary else ""
            summary = summary.strip()

    if not title:
        # Derive a title from the first words of the summary, or fall back.
        if summary:
            words = summary.split()[:6]
            title = " ".join(words).rstrip(".,;:!?")
        else:
            title = fallback_title

    return title, summary or "(no summary generated)"


def summarize_cluster(
    client,
    chat_model: str,
    member_texts: Sequence[str],
    *,
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
    max_bullet_tokens: int = DEFAULT_MAX_BULLET_TOKENS,
    max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    fallback_title: str = "Untitled cluster",
) -> SummarizationResult:
    """Ask Venice for a (title, summary) pair for a cluster of texts."""
    user_prompt, _ = _build_user_prompt(
        member_texts,
        max_context_tokens=max_context_tokens,
        max_bullet_tokens=max_bullet_tokens,
    )

    resp = client.chat.completions.create(
        model=chat_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
    )

    raw = resp.choices[0].message.content if resp.choices else ""
    raw = raw or ""
    title, summary = _parse_response(raw, fallback_title=fallback_title)
    return SummarizationResult(title=title, summary=summary, raw=raw)
