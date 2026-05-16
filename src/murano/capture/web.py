"""Web page capture (Phase 4).

Fetches a URL, extracts the main content with `trafilatura`, and writes a
Markdown file to `<vault>/web-captures/YYYY-MM-DD-<slug>.md` with YAML
frontmatter:

    ---
    title: <page title>
    source: <url>
    site: <site name>
    published: <YYYY-MM-DD or omitted>
    captured_at: <ISO 8601 UTC>
    tags:
      - web-capture
      - <user-supplied tags>
    ---

The watcher (`murano watch`) automatically picks up the new file; for the CLI
flow we also expose `capture_url(..., auto_index=True)` so a one-shot
`murano capture` immediately makes the page searchable.

Network policy: the only outbound calls Murano makes are to `api.venice.ai`
AND, in this module, to the URL the user explicitly asked to capture. No
third-party telemetry or analytics requests.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
import trafilatura

from ..config import Settings

USER_AGENT = "murano/0.1 (+https://github.com/aicaptains/murano)"
DEFAULT_TIMEOUT = 20.0
WEB_CAPTURE_SUBDIR = "web-captures"
DEFAULT_TAGS: tuple[str, ...] = ("web-capture",)
MAX_SLUG_LEN = 64


class CaptureError(RuntimeError):
    """Raised when a capture fails (network error, extraction empty, etc.)."""


@dataclass
class CapturedPage:
    """The result of a successful capture."""

    url: str
    title: str
    relpath: str  # vault-relative
    absolute_path: Path
    word_count: int
    byte_count: int
    site_name: str | None
    published_date: str | None


def slugify(text: str, *, max_len: int = MAX_SLUG_LEN) -> str:
    """Filesystem-safe, ASCII-only slug.

    Lowercases, NFKD-strips accents, collapses non-alphanumeric runs to a
    single hyphen, trims to `max_len`. Empty input becomes "untitled".
    """
    if not text:
        return "untitled"
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    if not cleaned:
        return "untitled"
    if len(cleaned) <= max_len:
        return cleaned
    # Truncate at a word boundary if possible.
    truncated = cleaned[:max_len]
    if "-" in truncated[max_len // 2 :]:
        truncated = truncated.rsplit("-", 1)[0]
    return truncated.strip("-") or "untitled"


def _slug_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parts:
        return slugify(parts[-1])
    return slugify(parsed.netloc or "untitled")


def _yaml_escape(value: str) -> str:
    """Minimal-but-safe YAML string escape for double-quoted scalars."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()


def _format_frontmatter(
    *,
    title: str,
    source_url: str,
    captured_at: datetime,
    site_name: str | None,
    published_date: str | None,
    tags: list[str],
) -> str:
    lines = ["---", f'title: "{_yaml_escape(title)}"', f'source: "{_yaml_escape(source_url)}"']
    if site_name:
        lines.append(f'site: "{_yaml_escape(site_name)}"')
    if published_date:
        lines.append(f'published: "{_yaml_escape(published_date)}"')
    lines.append(f'captured_at: "{captured_at.replace(microsecond=0).isoformat()}"')
    lines.append("tags:")
    for tag in tags:
        lines.append(f'  - "{_yaml_escape(tag)}"')
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _unique_path(target_dir: Path, base_slug: str, date_prefix: str, ext: str = ".md") -> Path:
    """Append `-2`, `-3`, ... to the slug if the target file already exists."""
    candidate = target_dir / f"{date_prefix}-{base_slug}{ext}"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = target_dir / f"{date_prefix}-{base_slug}-{n}{ext}"
        if not candidate.exists():
            return candidate
        n += 1
        if n > 999:
            raise CaptureError(
                f"Refusing to create more than 999 capture variants for slug {base_slug!r}"
            )


def fetch_html(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Download a URL via httpx with a sensible UA and follow redirects."""
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"},
            follow_redirects=True,
            timeout=timeout,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise CaptureError(f"Failed to fetch {url}: {e}") from e
    return resp.text


def extract_page(html: str, url: str) -> tuple[str, dict[str, str | None]]:
    """Run trafilatura's main-content extraction.

    Returns (markdown_body, metadata_dict). `metadata_dict` keys:
    `title`, `site_name`, `published_date`. Missing fields are `None`.
    """
    body = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_formatting=True,
        include_tables=True,
        include_images=False,
        include_comments=False,
        favor_precision=False,
    )
    if not body or not body.strip():
        raise CaptureError(
            f"trafilatura extracted no content from {url}. The page may require "
            "JavaScript, be behind a login/paywall, or have no main article body."
        )

    meta = trafilatura.extract_metadata(html, default_url=url)
    title = (meta.title or "").strip() if meta else ""
    site_name = (meta.sitename or "").strip() if meta else ""
    published = (meta.date or "").strip() if meta else ""
    return body.strip() + "\n", {
        "title": title or None,
        "site_name": site_name or None,
        "published_date": published or None,
    }


def capture_url(
    settings: Settings,
    url: str,
    *,
    extra_tags: list[str] | None = None,
    out_subdir: str = WEB_CAPTURE_SUBDIR,
    now: datetime | None = None,
    fetcher=None,
) -> CapturedPage:
    """Capture a URL into the vault as a Markdown file. Returns metadata about the write.

    `fetcher` is injected so tests can stub the network. If `None`, looks up
    `fetch_html` from this module at call time (so monkeypatching works).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CaptureError(
            f"Invalid URL {url!r}. Expected an absolute http(s) URL."
        )

    now = now or datetime.now(UTC)
    if fetcher is None:
        import sys as _sys

        fetcher = _sys.modules[__name__].fetch_html
    html = fetcher(url)
    body, meta = extract_page(html, url)

    title = meta["title"] or parsed.netloc + parsed.path or url
    base_slug = slugify(title) if meta["title"] else _slug_from_url(url)
    date_prefix = now.strftime("%Y-%m-%d")

    target_dir = settings.vault_root / out_subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = _unique_path(target_dir, base_slug, date_prefix)

    tags = list(DEFAULT_TAGS) + [t for t in (extra_tags or []) if t and t not in DEFAULT_TAGS]
    frontmatter = _format_frontmatter(
        title=title,
        source_url=url,
        captured_at=now,
        site_name=meta["site_name"],
        published_date=meta["published_date"],
        tags=tags,
    )

    full_text = frontmatter + body
    target_path.write_text(full_text, encoding="utf-8")

    relpath = str(target_path.relative_to(settings.vault_root))
    word_count = len(body.split())
    return CapturedPage(
        url=url,
        title=title,
        relpath=relpath,
        absolute_path=target_path,
        word_count=word_count,
        byte_count=len(full_text.encode("utf-8")),
        site_name=meta["site_name"],
        published_date=meta["published_date"],
    )
