"""Phase 6 — HTTP API tests using FastAPI's TestClient. Venice fully mocked."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from murano.api.server import create_app
from murano.config import Settings
from murano.index import db as chunks_db
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
def vault_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    vault.mkdir()
    (vault / "cooking").mkdir()
    data.mkdir()
    monkeypatch.setenv("MURANO_VAULT", str(vault))
    monkeypatch.setenv("MURANO_DATA", str(data))
    settings = Settings(vault_root=vault, data_root=data)

    # Seed a tiny indexed vault.
    (vault / "cooking" / "risotto.md").write_text(
        "# Mushroom Risotto\n\nSaute mushrooms, then add stock gradually.\n"
    )

    conn = chunks_db.connect(settings.chunks_db)
    chunks_db.init_for_model(conn, "fake-embed", EMBED_DIMS)
    chunks_db.upsert_file_with_chunks(
        conn,
        file_path="cooking/risotto.md",
        mtime=time.time(),
        file_hash="h",
        indexed_at=time.time(),
        chunks=[
            chunks_db.ChunkRow(
                id="cooking/risotto.md::0",
                file_path="cooking/risotto.md",
                ord=0,
                heading_path="Mushroom Risotto \u203a Method",
                content="Saute mushrooms, then add stock gradually.",
                content_hash="c1",
                token_count=8,
                byte_offset=0,
                embedding=_vec(1, 0, 0, 0, 0, 0, 0, 0),
            ),
            chunks_db.ChunkRow(
                id="cooking/risotto.md::1",
                file_path="cooking/risotto.md",
                ord=1,
                heading_path="Mushroom Risotto \u203a Ingredients",
                content="Arborio rice, mushrooms, parmesan, wine.",
                content_hash="c2",
                token_count=7,
                byte_offset=80,
                embedding=_vec(0, 1, 0, 0, 0, 0, 0, 0),
            ),
        ],
    )
    conn.close()
    return settings


@pytest.fixture
def client(vault_env: Settings):  # noqa: ARG001
    app = create_app(enable_schedule=False, enable_watch=False)
    with TestClient(app) as c:
        yield c


# --------- health ---------


def test_health_returns_paths_and_counts(client: TestClient, vault_env: Settings) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["vault_root"] == str(vault_env.vault_root)
    assert body["chunks_db_exists"] is True
    assert body["chunk_count"] == 2
    assert body["file_count"] == 1
    assert body["summary_tree_exists"] is False
    assert body["chat_model"] == "qwen-3-6-plus"


# --------- search (no LLM call) ---------


class _FakeClient:
    pass


def test_search_returns_ranked_hits(client: TestClient) -> None:
    with (
        patch("murano.chat.retriever.build_client", return_value=_FakeClient()),
        patch("murano.chat.retriever.resolve_models", return_value=_resolved()),
        patch("murano.chat.retriever.embed_one", return_value=_vec(0.9, 0.1, 0, 0, 0, 0, 0, 0)),
    ):
        r = client.post("/api/v1/search", json={"query": "how to cook risotto", "k": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["embed_model"] == "fake-embed"
    assert len(body["hits"]) == 2
    assert body["hits"][0]["citation"] == "[[cooking/risotto#Method]]"
    assert body["hits"][0]["distance"] < body["hits"][1]["distance"]


def test_search_400_on_empty_query(client: TestClient) -> None:
    r = client.post("/api/v1/search", json={"query": "", "k": 5})
    assert r.status_code == 422  # pydantic validation


# --------- ask (SSE) ---------


class _FakeDelta:
    def __init__(self, content):  # noqa: ANN001
        self.content = content


class _FakeChoice:
    def __init__(self, content, finish_reason=None):  # noqa: ANN001
        self.delta = _FakeDelta(content)
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, content, finish_reason=None):  # noqa: ANN001
        self.choices = [_FakeChoice(content, finish_reason)]


class _FakeCompletions:
    def __init__(self, pieces):
        self._pieces = pieces

    def create(self, **kwargs):
        assert kwargs["stream"] is True
        for p in self._pieces[:-1]:
            yield _FakeChunk(p)
        yield _FakeChunk(self._pieces[-1], finish_reason="stop")


class _FakeChat:
    def __init__(self, pieces):
        self.completions = _FakeCompletions(pieces)


class _FakeStreamingClient:
    def __init__(self, pieces):
        self.chat = _FakeChat(pieces)


def _parse_sse(raw_text: str) -> list[tuple[str, dict]]:
    """Tiny SSE parser for the test client output."""
    events: list[tuple[str, dict]] = []
    for chunk in raw_text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        event = "message"
        data_lines: list[str] = []
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())
        data_raw = "\n".join(data_lines)
        try:
            data = json.loads(data_raw)
        except json.JSONDecodeError:
            data = data_raw
        events.append((event, data))
    return events


def test_ask_streams_sse_events(client: TestClient) -> None:
    pieces = [
        "Use ", "arborio rice ", "[[cooking/risotto#Ingredients]]."
    ]
    with (
        patch("murano.chat.retriever.build_client", return_value=_FakeStreamingClient(pieces)),
        patch("murano.chat.retriever.resolve_models", return_value=_resolved()),
        patch("murano.chat.retriever.embed_one", return_value=_vec(0.9, 0.1, 0, 0, 0, 0, 0, 0)),
        client.stream(
            "POST",
            "/api/v1/ask",
            json={"query": "what rice for risotto?", "k": 2, "summary_k": 0},
        ) as resp,
    ):
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = "".join(chunk for chunk in resp.iter_text())

    events = _parse_sse(body)
    kinds = [e for e, _ in events]
    assert kinds[0] == "retrieval"
    assert kinds[-1] == "done"
    assert "delta" in kinds

    done_data = events[-1][1]
    assert done_data["finish_reason"] == "stop"
    assert "cooking/risotto#Ingredients" in done_data["cited"]
    assert "Use arborio rice" in done_data["text"]


def test_ask_409_when_no_index(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Override env to a vault dir with no chunks.db
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.setenv("MURANO_VAULT", str(fresh / "vault"))
    monkeypatch.setenv("MURANO_DATA", str(fresh / "data"))
    (fresh / "vault").mkdir()
    (fresh / "data").mkdir()
    r = client.post("/api/v1/ask", json={"query": "x"})
    assert r.status_code == 409
    assert "Run `murano index`" in r.json()["detail"]


# --------- chunks ---------


def test_get_chunk_returns_payload(client: TestClient) -> None:
    r = client.get("/api/v1/chunks/cooking/risotto.md::0")
    assert r.status_code == 200
    body = r.json()
    assert body["chunk_id"] == "cooking/risotto.md::0"
    assert body["heading_path"] == "Mushroom Risotto \u203a Method"


def test_get_chunk_404(client: TestClient) -> None:
    r = client.get("/api/v1/chunks/ghost.md::99")
    assert r.status_code == 404


# --------- themes (empty when no tree) ---------


def test_themes_empty_when_no_tree(client: TestClient) -> None:
    r = client.get("/api/v1/themes?level=1")
    assert r.status_code == 200
    assert r.json() == {"level": 1, "themes": []}


def test_themes_returns_nodes_when_tree_present(client: TestClient, vault_env: Settings) -> None:
    tconn = tree_db.connect(vault_env.summary_tree_db)
    try:
        tree_db.rebuild(
            tconn,
            nodes=[
                tree_db.TreeNodeRow(
                    id="L1::0",
                    level=1,
                    title="Risotto methods",
                    summary="A summary.",
                    member_count=2,
                    parent_id=None,
                    embedding=_vec(1, 0, 0, 0, 0, 0, 0, 0),
                )
            ],
            edges=[],
            embed_model="fake-embed",
            embed_dims=EMBED_DIMS,
            chat_model="fake-chat",
            source_chunk_count=2,
        )
    finally:
        tconn.close()
    r = client.get("/api/v1/themes?level=1")
    assert r.status_code == 200
    body = r.json()
    assert len(body["themes"]) == 1
    assert body["themes"][0]["title"] == "Risotto methods"


# --------- capture (mock fetcher + indexer) ---------


def test_capture_endpoint_writes_and_indexes(client: TestClient, vault_env: Settings) -> None:
    sample_html = """
    <html><head><title>Demo Article</title></head><body><article>
    <h1>Demo Article</h1>
    <p>A page with substantive text so trafilatura extracts it. Risotto is creamy.
    The Po Valley grows rice. Italians invented this dish.</p>
    <p>More text to convince trafilatura we have real content.</p>
    </article></body></html>
    """
    with (
        patch("murano.api.routes.capture_url") as cap_mock,
        patch("murano.api.routes.index_vault") as idx_mock,
    ):
        from murano.capture.web import CapturedPage

        cap_mock.return_value = CapturedPage(
            url="https://example.com/demo",
            title="Demo Article",
            relpath="web-captures/2026-05-16-demo-article.md",
            absolute_path=vault_env.vault_root / "web-captures/2026-05-16-demo-article.md",
            word_count=42,
            byte_count=512,
            site_name=None,
            published_date=None,
        )
        idx_mock.return_value = type("R", (), {"chunks_inserted": 3})()
        # The sample_html is unused since capture_url is mocked, but documents intent.
        _ = sample_html

        r = client.post(
            "/api/v1/capture",
            json={"url": "https://example.com/demo", "tags": ["demo"]},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == "https://example.com/demo"
    assert body["chunks_indexed"] == 3
    assert body["relpath"].startswith("web-captures/")


# --------- open (security) ---------


def test_open_rejects_path_outside_vault(client: TestClient) -> None:
    r = client.post("/api/v1/open", json={"path": "../../../etc/passwd"})
    assert r.status_code in (400, 404)


def test_open_404_for_missing_file(client: TestClient) -> None:
    r = client.post("/api/v1/open", json={"path": "does/not/exist.md"})
    assert r.status_code == 404


# --------- vault browser ---------


def test_vault_tree_lists_markdown_only(client: TestClient, vault_env: Settings) -> None:
    (vault_env.vault_root / "ignore.txt").write_text("nope")
    (vault_env.vault_root / "another.md").write_text("# Another\n\nstuff\n")
    r = client.get("/api/v1/vault/tree")
    assert r.status_code == 200
    body = r.json()
    names = {e["name"] for e in body["entries"]}
    assert "another.md" in names
    assert "cooking" in names  # dir is included because it has a .md inside
    assert "ignore.txt" not in names


def test_vault_file_returns_content(client: TestClient) -> None:
    r = client.get("/api/v1/vault/file?path=cooking/risotto.md")
    assert r.status_code == 200
    body = r.json()
    assert "Mushroom Risotto" in body["content"]


def test_vault_file_rejects_traversal(client: TestClient) -> None:
    r = client.get("/api/v1/vault/file?path=../../../etc/passwd")
    assert r.status_code in (400, 404)


# --------- UI pages render ---------


def test_pages_render_with_200(client: TestClient) -> None:
    for path in ("/", "/browse", "/settings"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Murano" in r.text


def test_static_assets_load(client: TestClient) -> None:
    css = client.get("/static/style.css")
    js = client.get("/static/app.js")
    assert css.status_code == 200
    assert "Murano" in css.text or "data-theme" in css.text
    assert js.status_code == 200
    assert "consumeSSE" in js.text


# --------- maintenance ---------


def test_ping_endpoint_returns_resolved_models(client: TestClient) -> None:
    with patch("murano.api.routes.resolve_models", return_value=_resolved()):
        r = client.post("/api/v1/ping")
    assert r.status_code == 200
    body = r.json()
    assert body["chat"]["resolved"] == "qwen-3-6-plus"
    assert body["embed"]["embedding_dimensions"] == EMBED_DIMS
