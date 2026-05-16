"""Phase 3.5 — MCP server tests.

These exercise the actual handler functions registered on the MCP Server
(both `list_tools` and `call_tool`) with Venice fully mocked. We avoid
spawning a stdio subprocess in pytest; that's done by the explicit smoke
test in the shell instead.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from murano.config import Settings
from murano.index import db as dbmod
from murano.mcp.server import (
    _build_server,
    _tool_ask,
    _tool_capture,
    _tool_get_chunk,
    _tool_list_themes,
    _tool_search,
)
from murano.tree import db as tree_db
from murano.venice import ResolvedModel, ResolvedModels

EMBED_DIMS = 8


def _vec(*xs: float) -> list[float]:
    assert len(xs) == EMBED_DIMS
    return list(xs)


def _resolved() -> ResolvedModels:
    return ResolvedModels(
        chat=ResolvedModel(requested="qwen-3-6-plus", resolved="qwen-3-6-plus", match="exact"),
        embed=ResolvedModel(
            requested="fake-embed",
            resolved="fake-embed",
            match="exact",
            embedding_dimensions=EMBED_DIMS,
            max_input_tokens=8192,
        ),
    )


@pytest.fixture
def vault_with_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    vault.mkdir()
    data.mkdir()
    monkeypatch.setenv("MURANO_VAULT", str(vault))
    monkeypatch.setenv("MURANO_DATA", str(data))

    settings = Settings(vault_root=vault, data_root=data)
    conn = dbmod.connect(settings.chunks_db)
    dbmod.init_for_model(conn, "fake-embed", EMBED_DIMS)
    dbmod.upsert_file_with_chunks(
        conn,
        file_path="cooking/risotto.md",
        mtime=time.time(),
        file_hash="h1",
        indexed_at=time.time(),
        chunks=[
            dbmod.ChunkRow(
                id="cooking/risotto.md::0",
                file_path="cooking/risotto.md",
                ord=0,
                heading_path="Mushroom Risotto \u203a Method",
                content="Saute the mushrooms, deglaze with wine, add stock gradually.",
                content_hash="cha",
                token_count=10,
                byte_offset=0,
                embedding=_vec(1, 0, 0, 0, 0, 0, 0, 0),
            ),
            dbmod.ChunkRow(
                id="cooking/risotto.md::1",
                file_path="cooking/risotto.md",
                ord=1,
                heading_path="Mushroom Risotto \u203a Ingredients",
                content="Arborio rice, cremini mushrooms, dry white wine, parmesan.",
                content_hash="chb",
                token_count=9,
                byte_offset=100,
                embedding=_vec(0, 1, 0, 0, 0, 0, 0, 0),
            ),
        ],
    )
    conn.close()
    return settings


def _patches_for_search():
    class _FakeClient:
        pass

    return [
        patch("murano.chat.retriever.build_client", return_value=_FakeClient()),
        patch("murano.chat.retriever.resolve_models", return_value=_resolved()),
        patch("murano.chat.retriever.embed_one", return_value=_vec(0.9, 0.1, 0, 0, 0, 0, 0, 0)),
    ]


class _FakeDelta:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None, finish_reason: str | None = None) -> None:
        self.delta = _FakeDelta(content)
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, content: str | None, finish_reason: str | None = None) -> None:
        self.choices = [_FakeChoice(content, finish_reason)]


class _FakeCompletions:
    def __init__(self, pieces: list[str]) -> None:
        self._pieces = pieces

    def create(self, **kwargs: Any):  # noqa: ANN401
        assert kwargs["stream"] is True
        for p in self._pieces[:-1]:
            yield _FakeChunk(p)
        yield _FakeChunk(self._pieces[-1], finish_reason="stop")


class _FakeChat:
    def __init__(self, pieces: list[str]) -> None:
        self.completions = _FakeCompletions(pieces)


class _FakeClientForAsk:
    def __init__(self, pieces: list[str]) -> None:
        self.chat = _FakeChat(pieces)


def _patches_for_ask(pieces: list[str]):
    return [
        patch("murano.chat.retriever.build_client", return_value=_FakeClientForAsk(pieces)),
        patch("murano.chat.retriever.resolve_models", return_value=_resolved()),
        patch("murano.chat.retriever.embed_one", return_value=_vec(0.9, 0.1, 0, 0, 0, 0, 0, 0)),
    ]


def test_build_server_registers_both_tools() -> None:
    app = _build_server()
    tools_request_handler = app.request_handlers.get(__import__("mcp.types", fromlist=["ListToolsRequest"]).ListToolsRequest)
    assert tools_request_handler is not None
    assert app.name == "murano"


def test_list_tools_returns_all_registered_tools() -> None:
    """Drive the registered list_tools handler through MCP's machinery."""
    import mcp.types as types

    app = _build_server()
    handler = app.request_handlers[types.ListToolsRequest]
    req = types.ListToolsRequest(method="tools/list")
    result_root = asyncio.run(handler(req))
    result = result_root.root
    assert isinstance(result, types.ListToolsResult)
    names = sorted(t.name for t in result.tools)
    assert names == [
        "ask_kb",
        "capture_url",
        "get_chunk",
        "list_themes",
        "search_kb",
    ]

    by_name = {t.name: t for t in result.tools}
    assert "query" in by_name["search_kb"].inputSchema["required"]
    assert "query" in by_name["ask_kb"].inputSchema["required"]
    assert "url" in by_name["capture_url"].inputSchema["required"]
    assert "chunk_id" in by_name["get_chunk"].inputSchema["required"]
    # list_themes has no required args (level defaults to 1)
    assert by_name["list_themes"].inputSchema.get("required", []) == []


def test_search_kb_returns_text_and_structured_json(vault_with_chunks: Settings) -> None:
    p1, p2, p3 = _patches_for_search()
    with p1, p2, p3:
        out = _tool_search({"query": "how do I make risotto", "k": 2})

    assert len(out) == 2
    text_payload = out[0].text
    assert "cooking/risotto.md" in text_payload
    assert "[[cooking/risotto#Method]]" in text_payload
    assert "[[cooking/risotto#Ingredients]]" in text_payload

    structured = json.loads(out[1].text)
    assert structured["embed_model"] == "fake-embed"
    assert len(structured["hits"]) == 2
    assert structured["hits"][0]["citation"] == "[[cooking/risotto#Method]]"
    assert structured["hits"][0]["distance"] < structured["hits"][1]["distance"]


def test_search_kb_clamps_k_to_safe_bounds(vault_with_chunks: Settings) -> None:
    p1, p2, p3 = _patches_for_search()
    with p1, p2, p3:
        out = _tool_search({"query": "rice", "k": 9999})
    structured = json.loads(out[1].text)
    assert len(structured["hits"]) <= 50


def test_search_kb_rejects_blank_query() -> None:
    with pytest.raises(ValueError, match="query"):
        _tool_search({"query": "   "})


def test_ask_kb_returns_answer_with_sources_footer(vault_with_chunks: Settings) -> None:
    pieces = [
        "To make risotto ",
        "saute the mushrooms first ",
        "[[cooking/risotto#Method]] ",
        "and use arborio rice ",
        "[[cooking/risotto#Ingredients]].",
    ]
    p1, p2, p3 = _patches_for_ask(pieces)
    with p1, p2, p3:
        out = _tool_ask({"query": "how do I make risotto", "k": 2})

    assert len(out) == 1
    body = out[0].text
    assert "To make risotto" in body
    assert "Sources:" in body
    assert "[cited]" in body
    assert "[[cooking/risotto#Method]]" in body
    assert "[[cooking/risotto#Ingredients]]" in body


def test_ask_kb_clamps_max_tokens_and_temperature(vault_with_chunks: Settings) -> None:
    pieces = ["Short answer ", "[[cooking/risotto#Method]]."]
    p1, p2, p3 = _patches_for_ask(pieces)
    with p1, p2, p3:
        out = _tool_ask(
            {
                "query": "rice",
                "k": 1,
                "max_tokens": 1_000_000,
                "temperature": 99.0,
            }
        )
    assert "[cited]" in out[0].text


def test_coerce_int_raises_on_non_numeric(vault_with_chunks: Settings) -> None:  # noqa: ARG001
    """Audit fix: bad types must surface as protocol errors, not silently clamp."""
    from murano.mcp.server import _coerce_int

    assert _coerce_int(None, default=10, lo=1, hi=50) == 10
    assert _coerce_int(5, default=10, lo=1, hi=50) == 5
    assert _coerce_int(999, default=10, lo=1, hi=50) == 50  # clamp ok
    assert _coerce_int(-1, default=10, lo=1, hi=50) == 1   # clamp ok
    with pytest.raises(ValueError, match="integer"):
        _coerce_int("not a number", default=10, lo=1, hi=50)
    with pytest.raises(ValueError, match="integer"):
        _coerce_int([1, 2, 3], default=10, lo=1, hi=50)


def test_coerce_float_raises_on_non_numeric() -> None:
    from murano.mcp.server import _coerce_float

    assert _coerce_float(None, default=0.5, lo=0.0, hi=2.0) == 0.5
    assert _coerce_float(1.5, default=0.5, lo=0.0, hi=2.0) == 1.5
    assert _coerce_float(99.0, default=0.5, lo=0.0, hi=2.0) == 2.0  # clamp
    with pytest.raises(ValueError, match="number"):
        _coerce_float("hot", default=0.5, lo=0.0, hi=2.0)
    with pytest.raises(ValueError, match="number"):
        _coerce_float({"x": 1}, default=0.5, lo=0.0, hi=2.0)


def test_call_tool_unknown_tool_is_protocol_error(vault_with_chunks: Settings) -> None:
    """Unknown tool name must yield CallToolResult(isError=True) per MCP protocol."""
    import mcp.types as types

    app = _build_server()
    handler = app.request_handlers[types.CallToolRequest]

    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="nonexistent_tool", arguments={"query": "x"}),
    )
    result_root = asyncio.run(handler(req))
    result = result_root.root
    assert isinstance(result, types.CallToolResult)
    assert result.isError is True
    assert any(
        "Unknown tool" in c.text for c in result.content if isinstance(c, types.TextContent)
    )


_SAMPLE_HTML = """
<html><head><title>Capture Demo Page</title></head><body>
<article>
<h1>Capture Demo</h1>
<p>This is a demonstration page. It has enough words for trafilatura to extract
the main body content. We mention risotto and arborio rice so search hits work.</p>
<p>Another paragraph here. More substance ensures the extractor treats this as
real content. Lorem ipsum dolor sit amet, consectetur adipiscing elit.</p>
</article>
</body></html>
"""


def test_capture_url_tool_writes_file_and_indexes_it(vault_with_chunks: Settings) -> None:
    from unittest.mock import patch as _patch

    # Stub the HTTP fetch + Venice embedding so the test is hermetic.
    p1, p2, p3 = _patches_for_search()
    with (
        p1, p2, p3,
        _patch("murano.capture.web.fetch_html", return_value=_SAMPLE_HTML),
        _patch("murano.index.indexer.embed_texts", side_effect=lambda *a, **k: [_vec(1,0,0,0,0,0,0,0)]),
        _patch("murano.index.indexer.build_client", return_value=object()),
        _patch("murano.index.indexer.resolve_models", return_value=_resolved()),
    ):
        out = _tool_capture({"url": "https://example.com/demo", "tags": ["demo", "test"]})

    # First content block = human text; second = structured JSON.
    assert len(out) == 2
    text_payload = out[0].text
    structured = json.loads(out[1].text)

    assert "Captured into the vault" in text_payload
    assert "indexed:" in text_payload
    assert structured["url"] == "https://example.com/demo"
    assert structured["relpath"].startswith("web-captures/")
    assert structured["relpath"].endswith(".md")
    assert structured["word_count"] > 0

    written_path = Path(structured["absolute_path"])
    assert written_path.exists()
    content = written_path.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert "demo" in content
    assert "test" in content


def test_capture_url_tool_rejects_blank_url(vault_with_chunks: Settings) -> None:
    with pytest.raises(ValueError, match="url"):
        _tool_capture({"url": "   "})


def test_capture_url_tool_rejects_non_list_tags(vault_with_chunks: Settings) -> None:
    with pytest.raises(ValueError, match="tags"):
        _tool_capture({"url": "https://example.com/x", "tags": "not-a-list"})


def _seed_tree(vault_with_chunks: Settings) -> None:
    """Insert a tiny tree directly into summary_tree.db (no LLM call needed)."""
    nodes = [
        tree_db.TreeNodeRow(
            id="L1::0",
            level=1,
            title="Risotto methods",
            summary="Notes on how to cook risotto well.",
            member_count=2,
            parent_id=None,
            embedding=_vec(1, 0, 0, 0, 0, 0, 0, 0),
        ),
        tree_db.TreeNodeRow(
            id="L1::1",
            level=1,
            title="Risotto ingredients",
            summary="Ingredients commonly used in mushroom risotto.",
            member_count=1,
            parent_id=None,
            embedding=_vec(0, 1, 0, 0, 0, 0, 0, 0),
        ),
    ]
    edges = [
        ("L1::0", "cooking/risotto.md::0", 0),
        ("L1::1", "cooking/risotto.md::1", 0),
    ]
    tconn = tree_db.connect(vault_with_chunks.summary_tree_db)
    try:
        tree_db.rebuild(
            tconn,
            nodes=nodes,
            edges=edges,
            embed_model="fake-embed",
            embed_dims=EMBED_DIMS,
            chat_model="fake-chat",
            source_chunk_count=2,
        )
    finally:
        tconn.close()


def test_list_themes_tool_returns_text_and_json(vault_with_chunks: Settings) -> None:
    _seed_tree(vault_with_chunks)
    out = _tool_list_themes({"level": 1})
    assert len(out) == 2
    text, structured_json = out[0].text, out[1].text
    assert "Risotto methods" in text
    assert "Risotto ingredients" in text
    payload = json.loads(structured_json)
    assert payload["level"] == 1
    assert {t["id"] for t in payload["themes"]} == {"L1::0", "L1::1"}


def test_list_themes_tool_helpful_when_tree_missing(vault_with_chunks: Settings) -> None:
    out = _tool_list_themes({"level": 1})
    assert len(out) == 1
    assert "No summary tree built yet" in out[0].text


def test_get_chunk_tool_returns_payload(vault_with_chunks: Settings) -> None:
    out = _tool_get_chunk({"chunk_id": "cooking/risotto.md::0"})
    assert len(out) == 2
    text, structured_json = out[0].text, out[1].text
    assert "cooking/risotto.md" in text
    assert "Saute the mushrooms" in text
    payload = json.loads(structured_json)
    assert payload["chunk_id"] == "cooking/risotto.md::0"
    assert payload["heading_path"] == "Mushroom Risotto \u203a Method"


def test_get_chunk_tool_missing_raises(vault_with_chunks: Settings) -> None:
    with pytest.raises(RuntimeError, match="not found"):
        _tool_get_chunk({"chunk_id": "ghost.md::99"})


def test_call_tool_search_kb_via_dispatcher(vault_with_chunks: Settings) -> None:
    """Full MCP machinery: CallToolRequest -> dispatched handler -> CallToolResult."""
    import mcp.types as types

    app = _build_server()
    handler = app.request_handlers[types.CallToolRequest]

    p1, p2, p3 = _patches_for_search()
    with p1, p2, p3:
        req = types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(
                name="search_kb", arguments={"query": "risotto", "k": 1}
            ),
        )
        result_root = asyncio.run(handler(req))

    result = result_root.root
    assert isinstance(result, types.CallToolResult)
    assert result.isError is not True
    assert len(result.content) >= 1
    text_blocks = [c.text for c in result.content if isinstance(c, types.TextContent)]
    assert any("cooking/risotto.md" in t for t in text_blocks)
