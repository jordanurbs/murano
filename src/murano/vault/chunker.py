"""Markdown-aware chunker.

Strategy (per MURANO_PLAN.md §11 Phase 2):

1. Strip leading YAML frontmatter (Obsidian-compatible) and capture it as a hint
   prepended to every chunk's `heading_path` (useful for citations).
2. Walk the document line-by-line, tracking the current H1/H2/H3 heading stack.
3. Split into *sections* at every H1/H2/H3 boundary. Each section is "everything
   under the most recent heading at the deepest level we split on".
4. For each section, token-count with the embedding model's tokenizer. If the
   section fits in `target_tokens`, emit it as a single chunk. If it overflows,
   split it into sub-chunks of `target_tokens` with `overlap_tokens` of overlap,
   preferring paragraph (blank-line) boundaries when possible.
5. Skip empty / whitespace-only chunks. Always carry the full `heading_path`
   ("Title › Section › Subsection") so retrieval can cite location precisely.

Tokenizer: `tiktoken.cl100k_base` — ~90% accurate vs Qwen3's real tokenizer,
zero heavy deps. Off-by-10% is irrelevant for chunk sizing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import tiktoken

DEFAULT_TARGET_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 64
HEADING_SEPARATOR = " \u203a "  # " › "

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TITLE_FRONTMATTER_RE = re.compile(r"^\s*title\s*:\s*(.+?)\s*$", re.MULTILINE)


@dataclass
class Chunk:
    """A single chunk produced by the chunker."""

    ord: int
    heading_path: str
    content: str
    token_count: int
    byte_offset: int

    @property
    def content_hash(self) -> str:
        h = hashlib.sha256()
        h.update(self.heading_path.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.content.encode("utf-8"))
        return h.hexdigest()


_encoder: tiktoken.Encoding | None = None


def _enc() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def count_tokens(text: str) -> int:
    return len(_enc().encode(text))


def file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _strip_frontmatter(text: str) -> tuple[str, str | None, int]:
    """Return (body, title_from_frontmatter_or_None, byte_offset_of_body)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return text, None, 0
    frontmatter = m.group(1)
    title_match = _TITLE_FRONTMATTER_RE.search(frontmatter)
    title = title_match.group(1).strip().strip("\"'") if title_match else None
    return text[m.end():], title, m.end()


def _build_heading_path(stack: list[tuple[int, str]], frontmatter_title: str | None) -> str:
    parts: list[str] = []
    if frontmatter_title:
        parts.append(frontmatter_title)
    parts.extend(title for _, title in stack)
    return HEADING_SEPARATOR.join(parts)


def _section_iter(
    text: str,
    frontmatter_title: str | None,
    split_max_level: int = 3,
) -> list[tuple[str, str, int]]:
    """Yield (heading_path, body_text, byte_offset_into_post_frontmatter) per section.

    A new section starts on every heading whose level <= split_max_level (so H1/H2/H3
    by default). H4-H6 stay inline as part of their parent section.
    """
    lines = text.splitlines(keepends=True)
    sections: list[tuple[str, str, int]] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    buf_start = 0
    cursor = 0

    def flush() -> None:
        body = "".join(buf).strip("\n")
        if body.strip():
            sections.append((_build_heading_path(stack, frontmatter_title), body, buf_start))

    for line in lines:
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) <= split_max_level:
            flush()
            buf = []
            buf_start = cursor + len(line) - len(line.lstrip(" \t"))
            level = len(m.group(1))
            title = m.group(2).strip()
            stack = [(lvl, t) for lvl, t in stack if lvl < level]
            stack.append((level, title))
        else:
            buf.append(line)
        cursor += len(line)

    flush()

    if not sections and text.strip():
        sections.append((_build_heading_path([], frontmatter_title), text.strip(), 0))

    return sections


def _split_oversized(
    body: str,
    target_tokens: int,
    overlap_tokens: int,
) -> list[tuple[str, int]]:
    """Split an oversized section into ~target_tokens sub-chunks with token overlap.

    Strategy: prefer paragraph boundaries (blank lines). Pack paragraphs into a
    sub-chunk until adding the next paragraph would overflow target_tokens, then
    emit and start the next sub-chunk with the last `overlap_tokens` worth of
    text from the previous chunk. Any single paragraph larger than target_tokens
    is hard-split on token boundaries.
    """
    enc = _enc()
    paragraphs = re.split(r"\n\s*\n", body)
    paragraphs = [p.strip("\n") for p in paragraphs if p.strip()]

    sub_chunks: list[tuple[str, int]] = []
    current: list[str] = []
    current_tokens = 0

    def flush_current(tail_overlap: str = "") -> None:
        nonlocal current, current_tokens
        if not current:
            return
        text = "\n\n".join(current)
        if tail_overlap:
            text = f"{tail_overlap}\n\n{text}" if text else tail_overlap
        sub_chunks.append((text, len(enc.encode(text))))
        current = []
        current_tokens = 0

    def take_tail(text: str, max_tokens: int) -> str:
        if max_tokens <= 0:
            return ""
        ids = enc.encode(text)
        if len(ids) <= max_tokens:
            return text
        return enc.decode(ids[-max_tokens:])

    overlap_text = ""
    for para in paragraphs:
        para_tokens = len(enc.encode(para))
        if para_tokens > target_tokens:
            flush_current(overlap_text)
            overlap_text = ""
            ids = enc.encode(para)
            step = max(1, target_tokens - overlap_tokens)
            for start in range(0, len(ids), step):
                window = ids[start : start + target_tokens]
                chunk_text = enc.decode(window)
                sub_chunks.append((chunk_text, len(window)))
                if start + target_tokens >= len(ids):
                    break
            overlap_text = take_tail(enc.decode(ids[-overlap_tokens:]), overlap_tokens)
            continue

        prospective = current_tokens + (2 if current else 0) + para_tokens
        if current and prospective > target_tokens:
            flush_current(overlap_text)
            overlap_text = take_tail(sub_chunks[-1][0], overlap_tokens)
        current.append(para)
        current_tokens = sum(len(enc.encode(p)) for p in current) + max(0, (len(current) - 1) * 2)

    flush_current(overlap_text)
    return sub_chunks


def chunk_markdown(
    text: str,
    *,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Chunk a Markdown document into Chunk records ready for embedding."""
    body, frontmatter_title, body_offset = _strip_frontmatter(text)
    sections = _section_iter(body, frontmatter_title)

    chunks: list[Chunk] = []
    ord_counter = 0
    for heading_path, section_body, section_offset in sections:
        tokens = count_tokens(section_body)
        absolute_offset = body_offset + section_offset
        if tokens <= target_tokens:
            chunks.append(
                Chunk(
                    ord=ord_counter,
                    heading_path=heading_path,
                    content=section_body,
                    token_count=tokens,
                    byte_offset=absolute_offset,
                )
            )
            ord_counter += 1
            continue
        for sub_text, sub_tokens in _split_oversized(section_body, target_tokens, overlap_tokens):
            chunks.append(
                Chunk(
                    ord=ord_counter,
                    heading_path=heading_path,
                    content=sub_text,
                    token_count=sub_tokens,
                    byte_offset=absolute_offset,
                )
            )
            ord_counter += 1

    return chunks
