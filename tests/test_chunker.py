"""Phase 2 — Markdown chunker tests."""

from __future__ import annotations

from murano.vault.chunker import (
    DEFAULT_OVERLAP_TOKENS,
    chunk_markdown,
    count_tokens,
    file_hash,
)


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_markdown("") == []
    assert chunk_markdown("\n   \n\n\t") == []


def test_single_section_under_cap_stays_whole() -> None:
    md = "# Hello\n\nThis is a single short section."
    chunks = chunk_markdown(md)
    assert len(chunks) == 1
    assert chunks[0].heading_path == "Hello"
    assert "single short section" in chunks[0].content
    assert chunks[0].token_count > 0
    assert chunks[0].ord == 0


def test_h1_h2_h3_split_with_heading_path() -> None:
    md = """# Title

intro

## Section A

alpha content

### Sub A1

a1 content

## Section B

bravo content

#### Deep H4

This H4 stays in section B.
"""
    chunks = chunk_markdown(md)
    paths = [c.heading_path for c in chunks]
    assert "Title" in paths
    assert "Title \u203a Section A" in paths
    assert "Title \u203a Section A \u203a Sub A1" in paths
    assert "Title \u203a Section B" in paths

    section_b = next(c for c in chunks if c.heading_path == "Title \u203a Section B")
    assert "#### Deep H4" in section_b.content
    assert "H4 stays in section B" in section_b.content


def test_frontmatter_title_prepended_to_heading_path() -> None:
    md = """---
title: My Note
tags: [a, b]
---

# Body Heading

body content
"""
    chunks = chunk_markdown(md)
    assert len(chunks) >= 1
    assert chunks[0].heading_path == "My Note \u203a Body Heading"


def test_oversized_section_is_split_with_overlap() -> None:
    paragraph = ("alpha bravo charlie delta echo foxtrot golf hotel " * 20).strip()
    body = "\n\n".join([paragraph] * 20)
    md = f"# Big Section\n\n{body}\n"
    chunks = chunk_markdown(md, target_tokens=128, overlap_tokens=16)

    assert len(chunks) >= 2
    for c in chunks:
        assert c.heading_path == "Big Section"
        assert c.token_count <= 128 + 16 + 8  # cap + overlap + small slack

    if len(chunks) >= 2:
        first_tail = " ".join(chunks[0].content.split()[-5:])
        second_head = " ".join(chunks[1].content.split()[:50])
        assert any(word in second_head for word in first_tail.split())


def test_oversized_single_paragraph_hard_split() -> None:
    md = "# Wall\n\n" + ("word " * 2000).strip()
    chunks = chunk_markdown(md, target_tokens=200, overlap_tokens=20)
    assert len(chunks) >= 2
    for c in chunks:
        assert c.token_count <= 200 + DEFAULT_OVERLAP_TOKENS


def test_chunks_have_ascending_ord_and_stable_hash() -> None:
    md = "# A\n\nalpha\n\n## B\n\nbeta\n\n## C\n\ngamma\n"
    chunks = chunk_markdown(md)
    ords = [c.ord for c in chunks]
    assert ords == sorted(ords)
    assert len(ords) == len(set(ords))

    again = chunk_markdown(md)
    assert [c.content_hash for c in chunks] == [c.content_hash for c in again]


def test_count_tokens_and_file_hash_basics() -> None:
    assert count_tokens("") == 0
    assert count_tokens("hello world") >= 1
    assert file_hash("abc") == file_hash("abc")
    assert file_hash("abc") != file_hash("abd")


def test_headings_with_brackets_dont_corrupt_citation_keys() -> None:
    """Audit-4 should-fix: a Markdown heading like `## Click [[here]] for more`
    flowed into heading_path unchanged. derive_citation_key produced
    `notes#Click [[here]] for more`; rendering as `[[...]]` gave
    `[[notes#Click [[here]] for more]]`, and the lazy regex matched the
    inner `[[here]]` instead of the whole thing — chunks showed uncited
    and phantom citations were reported as cited.

    Fix: strip brackets from heading_path segments at chunk time.
    """
    from murano.chat.answer import extract_citation_keys
    from murano.chat.retriever import derive_citation_key

    md = (
        "# Click [[here]] for more\n\n"
        "A paragraph of substantive text so the chunker keeps the section.\n"
    )
    chunks = chunk_markdown(md)
    assert chunks
    heading_path = chunks[0].heading_path
    # Brackets must not survive into heading_path.
    assert "[" not in heading_path
    assert "]" not in heading_path

    key = derive_citation_key("notes.md", heading_path)
    rendered = f"[[{key}]]"
    parsed = extract_citation_keys(rendered)
    # Round-trip: exactly one key, and it's the real one.
    assert len(parsed) == 1
    assert parsed[0] == key, (
        f"citation extraction round-trip failed; got {parsed}, expected [{key!r}]"
    )


def test_byte_offsets_are_true_utf8_bytes_not_characters() -> None:
    """Audit-3 should-fix: the chunker advanced `cursor` by `len(line)`
    (character count). For non-ASCII content above a heading, that desynced
    from real UTF-8 byte positions. After the fix, byte_offset points at
    the *body* of the section in real UTF-8 bytes.

    Reproducer text from the auditor:
        'éééé\\n# H\\nbody'
    UTF-8 bytes: éééé = 8 bytes, \\n = 1 (=9), # H = 3 (=12), \\n = 1 (=13),
    body = 4 (=17). The 'body' content begins at byte 13.
    """
    text = "éééé\n# H\nbody"
    body_bytes = text.encode("utf-8")
    assert len(body_bytes) == 17

    chunks = chunk_markdown(text)
    # The H section's body must start at UTF-8 byte 13 (== position of 'body').
    h_chunk = next(c for c in chunks if c.heading_path == "H")
    assert h_chunk.byte_offset == 13, (
        f"expected byte_offset to point at the 'body' UTF-8 byte (13); "
        f"got {h_chunk.byte_offset}. The chunker was using character offsets."
    )
    # Sanity: read the file at that offset and verify the content starts there.
    assert body_bytes[h_chunk.byte_offset:].decode("utf-8").startswith("body")
