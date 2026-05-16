"""Murano MCP server (Phase 3.5).

Exposes Murano's vault retrieval and RAG answer pipeline as MCP tools over stdio,
so any MCP-aware agent framework (Claude Desktop, Cursor, Hermes, OpenClaw,
Codex CLI, etc.) can use Murano as its persistent memory layer.

Tools exposed (per MURANO_PLAN.md §10):
    - search_kb(query, k=10) : top-K chunks with citations, no LLM call
    - ask_kb(query, k=6)     : full RAG answer with Obsidian-style citations
                               (synchronous from MCP's perspective; under the
                               hood it still streams from Venice and buffers)
    - capture_url(url, tags) : fetch a URL with trafilatura and write a
                               Markdown file with YAML frontmatter into the
                               vault; auto-indexes the new chunks
    - list_themes(level=1)   : walk the summary tree at a given level
    - get_chunk(chunk_id)    : fetch a single chunk by id from chunks.db

Logging discipline: MCP uses stdout for protocol messages, so all human-facing
logs go to stderr. The CLI wrapper sets that up before calling main().
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .. import __version__
from ..capture.web import CaptureError, capture_url
from ..chat.answer import StreamConfig, collect_answer, extract_citation_keys
from ..chat.retriever import Retriever
from ..config import load_settings
from ..index.indexer import index_vault
from ..tree.retrieve import get_chunk as tree_get_chunk
from ..tree.retrieve import list_themes as tree_list_themes
from ..tree.retrieve import status as tree_status
from ..venice import VeniceAuthError, VeniceConnectionError

SERVER_NAME = "murano"
SERVER_INSTRUCTIONS = (
    "Murano is a private, local-first personal knowledge base. "
    "Use `search_kb` for raw top-K chunks (when you want to read citations "
    "yourself) and `ask_kb` for a full LLM-grounded answer with inline "
    "Obsidian-style citations ([[file#heading]])."
)


def _build_server() -> Server:
    app: Server = Server(
        name=SERVER_NAME,
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
    )

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="search_kb",
                title="Search knowledge base",
                description=(
                    "Vector search over the Murano vault. Returns the top-K "
                    "Markdown chunks ranked by semantic similarity to the query, "
                    "each annotated with its file path, heading path, and an "
                    "Obsidian-style citation key (`[[file#heading]]`). "
                    "Does not call an LLM; cheap and fast."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The natural-language search query.",
                        },
                        "k": {
                            "type": "integer",
                            "description": "How many top hits to return.",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="ask_kb",
                title="Ask knowledge base",
                description=(
                    "Full RAG: retrieves top-K chunks from the vault then asks "
                    "the configured Venice chat model to answer the question, "
                    "grounded only in those chunks, with inline "
                    "`[[file#heading]]` citations. Use this when you want a "
                    "synthesised answer; use `search_kb` when you want raw "
                    "passages."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The question to ask.",
                        },
                        "k": {
                            "type": "integer",
                            "description": "How many chunks to retrieve as context.",
                            "default": 6,
                            "minimum": 1,
                            "maximum": 20,
                        },
                        "max_tokens": {
                            "type": "integer",
                            "description": "Maximum answer length in tokens.",
                            "default": 1024,
                            "minimum": 16,
                            "maximum": 8192,
                        },
                        "temperature": {
                            "type": "number",
                            "description": "Sampling temperature for the chat model.",
                            "default": 0.2,
                            "minimum": 0.0,
                            "maximum": 2.0,
                        },
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="capture_url",
                title="Capture URL into vault",
                description=(
                    "Fetches a web page, extracts its main content with "
                    "trafilatura, and writes a Markdown file with YAML "
                    "frontmatter into `<vault>/web-captures/`. The file is "
                    "indexed immediately, so subsequent `ask_kb` and "
                    "`search_kb` calls can cite it."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "Absolute http(s) URL to capture.",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional extra tags added to the file's "
                                "frontmatter alongside `web-capture`."
                            ),
                            "default": [],
                        },
                    },
                    "required": ["url"],
                },
            ),
            types.Tool(
                name="list_themes",
                title="List vault themes (summary tree)",
                description=(
                    "Walk the Murano summary tree at a given level. Returns "
                    "every cluster summary (title + 3-5 sentence summary + "
                    "member count). Useful to orient an agent before deciding "
                    "what to ask about. Returns an empty list if no tree has "
                    "been built (run `murano tree rebuild`)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "level": {
                            "type": "integer",
                            "description": (
                                "Which level to list. Level 1 is the most "
                                "granular (clusters of chunks). Higher levels "
                                "are clusters of summaries. Default: 1."
                            ),
                            "default": 1,
                            "minimum": 1,
                            "maximum": 6,
                        },
                    },
                },
            ),
            types.Tool(
                name="get_chunk",
                title="Get a specific chunk by id",
                description=(
                    "Fetch the full content + heading path of a single chunk "
                    "from the vault index. `chunk_id` has the form "
                    "`<file-relpath>::<ord>`, as returned by `search_kb` hits "
                    "and `ask_kb` Sources footers."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chunk_id": {
                            "type": "string",
                            "description": "The chunk id, e.g. `cooking/risotto.md::2`.",
                        },
                    },
                    "required": ["chunk_id"],
                },
            ),
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        # Per MCP protocol, raising here yields a CallToolResult(isError=True)
        # with the exception message as content — which is exactly what we want.
        # We translate Murano's own error types to RuntimeErrors so the message
        # is actionable instead of dumping a stack-traceable type name.
        try:
            if name == "search_kb":
                return _tool_search(arguments)
            if name == "ask_kb":
                return _tool_ask(arguments)
            if name == "capture_url":
                return _tool_capture(arguments)
            if name == "list_themes":
                return _tool_list_themes(arguments)
            if name == "get_chunk":
                return _tool_get_chunk(arguments)
            raise ValueError(f"Unknown tool: {name!r}")
        except VeniceAuthError as e:
            raise RuntimeError(f"Murano is not configured: {e}") from e
        except VeniceConnectionError as e:
            raise RuntimeError(f"Venice connection failed: {e}") from e
        except CaptureError as e:
            raise RuntimeError(f"Capture failed: {e}") from e

    return app


def _tool_search(arguments: dict[str, Any]) -> list[types.TextContent]:
    query = _require_str(arguments, "query")
    k = _coerce_int(arguments.get("k", 10), default=10, lo=1, hi=50)

    settings = load_settings()
    _ensure_index_present(settings)

    with Retriever.open(settings) as r:
        result = r.retrieve(query, k=k)

    if not result.hits:
        return [
            types.TextContent(
                type="text",
                text=(
                    "No matches in the vault. Run `murano index` after dropping "
                    "Markdown files into ~/murano/vault/."
                ),
            )
        ]

    text_lines: list[str] = [
        f"Found {len(result.hits)} chunks (embed={result.embed_model}, "
        f"retrieval={result.elapsed_ms:.0f}ms):",
        "",
    ]
    structured_hits: list[dict[str, Any]] = []
    for i, h in enumerate(result.hits, start=1):
        text_lines.append(
            f"[{i}] {h.file_path}"
            + (f"  ({h.heading_path})" if h.heading_path else "")
            + f"  distance={h.distance:.4f}"
        )
        text_lines.append(f"    cite: [[{h.citation_key}]]")
        text_lines.append(f"    excerpt: {h.content.strip()}")
        text_lines.append("")
        structured_hits.append(
            {
                "rank": i,
                "chunk_id": h.chunk_id,
                "file_path": h.file_path,
                "heading_path": h.heading_path,
                "citation": f"[[{h.citation_key}]]",
                "distance": h.distance,
                "token_count": h.token_count,
                "content": h.content,
            }
        )

    text_body = "\n".join(text_lines).rstrip() + "\n"
    structured_body = json.dumps(
        {
            "query": result.query,
            "embed_model": result.embed_model,
            "elapsed_ms": result.elapsed_ms,
            "hits": structured_hits,
        },
        ensure_ascii=False,
        indent=2,
    )
    return [
        types.TextContent(type="text", text=text_body),
        types.TextContent(type="text", text=structured_body),
    ]


def _tool_ask(arguments: dict[str, Any]) -> list[types.TextContent]:
    query = _require_str(arguments, "query")
    k = _coerce_int(arguments.get("k", 6), default=6, lo=1, hi=20)
    max_tokens = _coerce_int(arguments.get("max_tokens", 1024), default=1024, lo=16, hi=8192)
    temperature = _coerce_float(
        arguments.get("temperature", 0.2), default=0.2, lo=0.0, hi=2.0
    )

    settings = load_settings()
    _ensure_index_present(settings)

    cfg = StreamConfig(k=k, max_tokens=max_tokens, temperature=temperature)
    answer_text, retrieval = collect_answer(settings, query, config=cfg)
    cited_keys = set(extract_citation_keys(answer_text))

    sources_lines = ["", "---", "Sources:"]
    for i, h in enumerate(retrieval.hits, start=1):
        mark = "[cited]" if h.citation_key in cited_keys else "       "
        heading = f"  ({h.heading_path})" if h.heading_path else ""
        sources_lines.append(
            f"  {mark} [{i}] {h.file_path}{heading}  [[{h.citation_key}]]"
        )

    body = answer_text.rstrip() + "\n" + "\n".join(sources_lines)
    return [types.TextContent(type="text", text=body)]


def _tool_capture(arguments: dict[str, Any]) -> list[types.TextContent]:
    url = _require_str(arguments, "url")
    raw_tags = arguments.get("tags") or []
    if not isinstance(raw_tags, list):
        raise ValueError("Argument `tags` must be an array of strings if supplied.")
    extra_tags = [str(t).strip() for t in raw_tags if str(t).strip()]

    settings = load_settings()
    if not settings.vault_root.exists():
        raise RuntimeError(
            f"Vault does not exist at {settings.vault_root}. Run `murano init` first."
        )

    page = capture_url(settings, url, extra_tags=extra_tags or None)

    text_lines = [
        "Captured into the vault:",
        f"  title:    {page.title}",
        f"  path:     {page.relpath}",
        f"  words:    {page.word_count}",
        f"  size:     {page.byte_count} bytes",
    ]
    if page.site_name:
        text_lines.append(f"  site:     {page.site_name}")
    if page.published_date:
        text_lines.append(f"  published: {page.published_date}")

    # Auto-index so the page is immediately queryable. Index failures fall
    # back to a warning — the file itself is already on disk.
    try:
        report = index_vault(settings, subpath=Path(page.relpath))
        text_lines.append(
            f"  indexed:  {report.chunks_inserted} chunks "
            f"({report.elapsed_seconds:.2f}s)"
        )
    except VeniceAuthError as e:
        text_lines.append(f"  index:    NOT indexed ({e})")
    except VeniceConnectionError as e:
        text_lines.append(f"  index:    NOT indexed ({e})")

    structured = json.dumps(
        {
            "url": page.url,
            "title": page.title,
            "relpath": page.relpath,
            "absolute_path": str(page.absolute_path),
            "word_count": page.word_count,
            "byte_count": page.byte_count,
            "site_name": page.site_name,
            "published_date": page.published_date,
        },
        ensure_ascii=False,
        indent=2,
    )
    return [
        types.TextContent(type="text", text="\n".join(text_lines)),
        types.TextContent(type="text", text=structured),
    ]


def _tool_list_themes(arguments: dict[str, Any]) -> list[types.TextContent]:
    level = _coerce_int(arguments.get("level", 1), default=1, lo=1, hi=6)
    settings = load_settings()
    nodes = tree_list_themes(settings, level=level)
    if not nodes:
        st = tree_status(settings)
        if not st.exists:
            return [
                types.TextContent(
                    type="text",
                    text=(
                        "No summary tree built yet. Run `murano tree rebuild` "
                        "after indexing your vault."
                    ),
                )
            ]
        return [
            types.TextContent(
                type="text",
                text=(
                    f"No nodes at level {level}. Tree has levels: "
                    f"{', '.join(str(lv) for lv in st.levels)}."
                ),
            )
        ]

    text_lines = [f"Level {level}: {len(nodes)} theme(s)", ""]
    structured = []
    for n in nodes:
        text_lines.append(f"[{n.id}] {n.title}  ({n.member_count} members)")
        for line in n.summary.splitlines():
            text_lines.append(f"    {line}")
        text_lines.append("")
        structured.append(
            {
                "id": n.id,
                "level": n.level,
                "title": n.title,
                "summary": n.summary,
                "member_count": n.member_count,
                "parent_id": n.parent_id,
            }
        )

    return [
        types.TextContent(type="text", text="\n".join(text_lines).rstrip() + "\n"),
        types.TextContent(
            type="text",
            text=json.dumps({"level": level, "themes": structured}, ensure_ascii=False, indent=2),
        ),
    ]


def _tool_get_chunk(arguments: dict[str, Any]) -> list[types.TextContent]:
    chunk_id = _require_str(arguments, "chunk_id")
    settings = load_settings()
    if not settings.chunks_db.exists():
        raise RuntimeError(
            f"No index found at {settings.chunks_db}. Run `murano index` first."
        )
    rec = tree_get_chunk(settings, chunk_id)
    if rec is None:
        raise RuntimeError(f"Chunk not found: {chunk_id!r}")
    text_payload = (
        f"chunk_id:    {rec.chunk_id}\n"
        f"file:        {rec.file_path}\n"
        f"heading:     {rec.heading_path or '(none)'}\n"
        f"tokens:      {rec.token_count}\n"
        f"byte_offset: {rec.byte_offset}\n"
        f"---\n"
        f"{rec.content}\n"
    )
    structured = json.dumps(
        {
            "chunk_id": rec.chunk_id,
            "file_path": rec.file_path,
            "ord": rec.ord,
            "heading_path": rec.heading_path,
            "token_count": rec.token_count,
            "byte_offset": rec.byte_offset,
            "content": rec.content,
        },
        ensure_ascii=False,
        indent=2,
    )
    return [
        types.TextContent(type="text", text=text_payload),
        types.TextContent(type="text", text=structured),
    ]


def _require_str(arguments: dict[str, Any], key: str) -> str:
    val = arguments.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"Required string argument {key!r} missing or empty.")
    return val.strip()


def _coerce_int(val: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _coerce_float(val: Any, *, default: float, lo: float, hi: float) -> float:
    try:
        f = float(val)
    except (TypeError, ValueError):
        f = default
    return max(lo, min(hi, f))


def _ensure_index_present(settings) -> None:  # noqa: ANN001
    if not settings.chunks_db.exists():
        raise RuntimeError(
            f"No index found at {settings.chunks_db}. "
            "Run `murano index` after dropping Markdown files into "
            f"{settings.vault_root}."
        )


async def _main_async() -> None:
    app = _build_server()
    async with stdio_server() as (read_stream, write_stream):
        print("[murano-mcp] ready on stdio", file=sys.stderr, flush=True)
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main() -> None:
    """Sync entrypoint used by the CLI."""
    import asyncio

    asyncio.run(_main_async())
