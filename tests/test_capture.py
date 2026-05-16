"""Phase 4 — web capture tests. Network is fully mocked via a fake fetcher."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from murano.capture.web import (
    CaptureError,
    _format_frontmatter,
    _unique_path,
    capture_url,
    extract_page,
    slugify,
)
from murano.config import Settings


def test_slugify_basics() -> None:
    assert slugify("Hello World") == "hello-world"
    assert slugify("  Risotto, mushroom!  ") == "risotto-mushroom"
    assert slugify("Café au lait") == "cafe-au-lait"
    assert slugify("---") == "untitled"
    assert slugify("") == "untitled"


def test_slugify_truncates_at_word_boundary() -> None:
    long = "the quick brown fox jumps over the lazy dog and keeps on going forever and ever"
    s = slugify(long, max_len=40)
    assert len(s) <= 40
    assert not s.endswith("-")
    assert s.startswith("the-quick-brown-fox")


def test_unique_path_appends_numeric_suffix(tmp_path: Path) -> None:
    d = tmp_path / "cap"
    d.mkdir()
    first = _unique_path(d, "foo", "2026-05-16")
    first.write_text("x")
    second = _unique_path(d, "foo", "2026-05-16")
    second.write_text("x")
    third = _unique_path(d, "foo", "2026-05-16")
    assert first.name == "2026-05-16-foo.md"
    assert second.name == "2026-05-16-foo-2.md"
    assert third.name == "2026-05-16-foo-3.md"


def test_format_frontmatter_writes_expected_keys() -> None:
    ts = datetime(2026, 5, 16, 14, 30, 0, tzinfo=UTC)
    out = _format_frontmatter(
        title='Risotto: a "classic"',
        source_url="https://example.com/risotto?id=42",
        captured_at=ts,
        site_name="Example Site",
        published_date="2024-09-01",
        tags=["web-capture", "cooking"],
    )
    assert out.startswith("---\n")
    assert out.endswith("---\n\n") or out.endswith("---\n")
    assert 'title: "Risotto: a \\"classic\\""' in out
    assert 'source: "https://example.com/risotto?id=42"' in out
    assert 'site: "Example Site"' in out
    assert 'published: "2024-09-01"' in out
    assert 'captured_at: "2026-05-16T14:30:00+00:00"' in out
    assert '  - "web-capture"' in out
    assert '  - "cooking"' in out


def test_format_frontmatter_omits_optional_fields_when_absent() -> None:
    ts = datetime(2026, 5, 16, 14, 30, 0, tzinfo=UTC)
    out = _format_frontmatter(
        title="x",
        source_url="https://example.com/",
        captured_at=ts,
        site_name=None,
        published_date=None,
        tags=["web-capture"],
    )
    assert "site:" not in out
    assert "published:" not in out


def test_extract_page_returns_body_and_metadata() -> None:
    html = """
    <html><head>
      <title>Test Article Title</title>
      <meta property="og:site_name" content="Test Site">
      <meta property="article:published_time" content="2025-03-14T12:00:00Z">
    </head><body>
    <article>
      <h1>The Real Article Heading</h1>
      <p>This is the first paragraph of the article body. It is long enough that trafilatura
      considers it real content and not boilerplate. We need a few sentences so the
      extraction heuristics succeed.</p>
      <h2>Second Section</h2>
      <p>More content lives here, with enough words to keep trafilatura interested in
      considering this a content-bearing page worth extracting.</p>
      <p>Even more substance for good measure. Risotto is a creamy Italian dish.</p>
    </article>
    </body></html>
    """
    body, meta = extract_page(html, "https://example.com/article")
    assert "The Real Article Heading" in body or "Real Article" in body
    # trafilatura prefers <h1> over <title> when both exist
    assert meta["title"] is not None
    assert "Real Article" in (meta["title"] or "") or "Test Article" in (meta["title"] or "")


def test_extract_page_raises_when_no_content_found() -> None:
    html = "<html><body></body></html>"
    with pytest.raises(CaptureError, match="extracted no content"):
        extract_page(html, "https://example.com/empty")


@pytest.fixture
def vault(tmp_path: Path) -> Settings:
    v = tmp_path / "vault"
    d = tmp_path / "data"
    v.mkdir()
    d.mkdir()
    return Settings(vault_root=v, data_root=d)


def test_capture_url_rejects_non_http_urls(vault: Settings) -> None:
    for bad in ("file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "", "not-a-url"):
        with pytest.raises(CaptureError, match="Invalid URL"):
            capture_url(vault, bad)


_SAMPLE_HTML = """
<html><head>
  <title>Mushroom Risotto Recipe</title>
  <meta property="og:site_name" content="Cooking Site">
  <meta property="article:published_time" content="2024-09-01T10:00:00Z">
</head><body>
<article>
  <h1>Mushroom Risotto</h1>
  <p>This is a classic Italian risotto with cremini mushrooms and arborio rice.
  Cook it slowly, one ladle of stock at a time, until the rice is al dente.
  Serve with grated parmesan and fresh thyme on top of each portion.</p>
  <h2>Ingredients</h2>
  <p>You will need arborio rice, cremini mushrooms, dry white wine, vegetable stock,
  butter, olive oil, a small shallot, and a generous handful of grated parmesan cheese.</p>
  <h2>Method</h2>
  <p>Saute the mushrooms first in olive oil over high heat. Set them aside, then
  toast the rice in butter and shallot, deglaze with the wine, and add the warm stock
  one ladle at a time, stirring constantly until each is absorbed before the next.</p>
</article>
</body></html>
"""


def test_capture_url_writes_file_with_frontmatter_and_tags(vault: Settings) -> None:
    def fake_fetch(url: str) -> str:  # noqa: ARG001
        return _SAMPLE_HTML

    now = datetime(2026, 5, 16, 14, 30, 0, tzinfo=UTC)
    page = capture_url(
        vault,
        "https://example.com/risotto",
        extra_tags=["cooking", "italian"],
        now=now,
        fetcher=fake_fetch,
    )

    assert page.absolute_path.exists()
    assert page.relpath.startswith("web-captures/2026-05-16-")
    assert page.relpath.endswith(".md")
    assert page.title.startswith("Mushroom Risotto")
    assert page.word_count > 0
    assert page.site_name == "Cooking Site"
    assert page.published_date and page.published_date.startswith("2024-09-01")

    content = page.absolute_path.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert 'source: "https://example.com/risotto"' in content
    assert 'site: "Cooking Site"' in content
    assert '  - "web-capture"' in content
    assert '  - "cooking"' in content
    assert '  - "italian"' in content
    body_after_frontmatter = content.split("---\n", 2)[-1]
    assert "Mushroom Risotto" in body_after_frontmatter


def test_capture_url_uses_url_slug_when_title_missing(vault: Settings) -> None:
    html_no_title = """
    <html><body><article>
    <p>A page with no title metadata at all but plenty of body content here.
    Trafilatura should still extract this text as the main content of the page,
    even though there is no h1 or title element to pull a name from.</p>
    <p>More text here so the extraction heuristics consider this an article.</p>
    </article></body></html>
    """

    def fake_fetch(url: str) -> str:  # noqa: ARG001
        return html_no_title

    now = datetime(2026, 5, 16, tzinfo=UTC)
    page = capture_url(
        vault,
        "https://example.com/some/path/long-article-name",
        now=now,
        fetcher=fake_fetch,
    )
    assert "long-article-name" in page.relpath


def test_capture_url_collision_appends_suffix(vault: Settings) -> None:
    def fake_fetch(url: str) -> str:  # noqa: ARG001
        return _SAMPLE_HTML

    now = datetime(2026, 5, 16, 14, 30, 0, tzinfo=UTC)
    p1 = capture_url(vault, "https://example.com/risotto", now=now, fetcher=fake_fetch)
    p2 = capture_url(vault, "https://example.com/risotto", now=now, fetcher=fake_fetch)
    p3 = capture_url(vault, "https://example.com/risotto", now=now, fetcher=fake_fetch)
    assert p1.relpath != p2.relpath != p3.relpath
    assert p1.absolute_path.exists()
    assert p2.absolute_path.exists()
    assert p3.absolute_path.exists()
    assert "-2.md" in p2.relpath
    assert "-3.md" in p3.relpath


def test_capture_url_raises_when_extraction_empty(vault: Settings) -> None:
    def fake_fetch(url: str) -> str:  # noqa: ARG001
        return "<html><body></body></html>"

    with pytest.raises(CaptureError, match="extracted no content"):
        capture_url(vault, "https://example.com/empty", fetcher=fake_fetch)


class _FakeStreamResp:
    """Reusable httpx.Client.stream() context-manager double."""

    def __init__(self, *, status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = "utf-8"
        self._chunks = chunks or []

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass
    def iter_bytes(self):
        yield from self._chunks


def _patch_client_stream(monkeypatch: pytest.MonkeyPatch, resp_factory) -> None:
    """Replace httpx.Client(...).stream(...) with a factory returning a fake response.

    fetch_html now drives redirects manually via `httpx.Client`, so the prior
    `httpx.stream` mock no longer applies. resp_factory(method, url, **kw) ->
    _FakeStreamResp.
    """
    import httpx

    class _FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url, **kw):
            return resp_factory(method, url, **kw)

    monkeypatch.setattr(httpx, "Client", _FakeClient)


def test_fetch_html_streams_and_caps_huge_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit-3 polish: prior `httpx.get(...).text` buffered the entire body
    unbounded, so a malicious huge response could OOM `murano capture`."""
    from murano.capture.web import CaptureError, fetch_html

    chunk = b"x" * 1024
    _patch_client_stream(
        monkeypatch,
        lambda *a, **k: _FakeStreamResp(chunks=[chunk] * 50),
    )

    with pytest.raises(CaptureError, match="exceeded"):
        fetch_html("https://example.com/", max_bytes=8192, enforce_public_host=False)


def test_fetch_html_rejects_oversized_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server-declared Content-Length larger than the cap is rejected
    BEFORE we start buffering anything."""
    from murano.capture.web import CaptureError, fetch_html

    def factory(*a, **k):
        return _FakeStreamResp(
            headers={"content-length": str(100 * 1024 * 1024)},
            chunks=[],  # would AssertionError if we got here, but we shouldn't
        )

    _patch_client_stream(monkeypatch, factory)
    with pytest.raises(CaptureError, match="Content-Length"):
        fetch_html(
            "https://example.com/",
            max_bytes=16 * 1024 * 1024,
            enforce_public_host=False,
        )


def test_capture_and_index_returns_index_skip_reason_on_venice_failure(
    vault: Settings,
) -> None:
    """Audit fix: the "capture then index, tolerate Venice errors" policy
    was duplicated across CLI / HTTP / MCP. The shared helper must return
    a sentinel + reason instead of leaking exceptions."""
    from unittest.mock import patch

    from murano.capture.web import capture_and_index
    from murano.venice import VeniceAuthError

    def fake_fetch(url: str) -> str:  # noqa: ARG001
        return _SAMPLE_HTML

    with patch(
        "murano.index.indexer.build_client",
        side_effect=VeniceAuthError("no key in keychain"),
    ):
        result = capture_and_index(
            vault,
            "https://example.com/risotto",
            fetcher=fake_fetch,
        )

    assert result.page.absolute_path.exists()
    assert result.chunks_indexed == -1
    assert result.index_skipped_reason is not None
    assert "no key" in result.index_skipped_reason.lower()
