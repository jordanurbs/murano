"""Murano MCP server (Phase 3.5).

Exposes Murano's vault retrieval and RAG answer pipeline as MCP tools over stdio,
so any MCP-aware agent framework (Claude Desktop, Cursor, Hermes, OpenClaw,
Codex CLI, etc.) can use Murano as its persistent memory layer.

Tools exposed in this phase (per MURANO_PLAN.md §10):
    - search_kb(query, k=10) : top-K chunks with citations, no LLM call
    - ask_kb(query, k=6)     : full RAG answer with Obsidian-style citations
                               (synchronous from MCP's perspective; under the
                               hood it still streams from Venice and buffers)

Tools deferred to later phases:
    - capture_url(url)       : Phase 4 (web capture)
    - list_themes(level=1)   : Phase 5 (summary tree)
    - get_chunk(id)          : Phase 5

Logging discipline: MCP uses stdout for protocol messages, so all human-facing
logs go to stderr. The CLI wrapper sets that up before calling main().
"""

from __future__ import annotations

import json
import sys
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .. import __version__
from ..chat.answer import StreamConfig, collect_answer, extract_citation_keys
from ..chat.retriever import Retriever
from ..config import load_settings
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
            raise ValueError(f"Unknown tool: {name!r}")
        except VeniceAuthError as e:
            raise RuntimeError(f"Murano is not configured: {e}") from e
        except VeniceConnectionError as e:
            raise RuntimeError(f"Venice connection failed: {e}") from e

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
