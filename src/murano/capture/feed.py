"""RSS / Atom feed capture.

Fetches a feed, walks its entries, and captures each entry URL into the vault
via the existing `capture_url` pipeline. Honors a `--limit` so a noisy feed
doesn't accidentally pull thousands of articles into your vault.

We track per-feed "last seen entry id/link" in `~/.murano/logs/feeds.json` so
re-running `murano capture-feed` only ingests new items. Reset the state file
to re-ingest from scratch.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import feedparser

from ..config import Settings
from .web import CapturedPage, CaptureError, capture_url

DEFAULT_LIMIT = 20
STATE_FILENAME = "feeds.json"

_logger = logging.getLogger("murano.capture.feed")


class FeedError(RuntimeError):
    """Raised when feed fetching or parsing fails."""


@dataclass
class FeedEntryResult:
    url: str
    title: str
    status: str  # "captured" | "seen" | "error"
    relpath: str | None = None
    error: str | None = None


@dataclass
class FeedReport:
    feed_url: str
    feed_title: str
    entries_total: int
    captured: list[FeedEntryResult] = field(default_factory=list)
    seen: list[FeedEntryResult] = field(default_factory=list)
    errors: list[FeedEntryResult] = field(default_factory=list)


def _state_path(settings: Settings) -> Path:
    return settings.logs_dir / STATE_FILENAME


def _load_state(settings: Settings) -> dict[str, dict[str, Any]]:
    p = _state_path(settings)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(settings: Settings, state: dict[str, dict[str, Any]]) -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    _state_path(settings).write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _entry_id(entry: Any) -> str:
    """Best-effort stable id for a feed entry."""
    for attr in ("id", "guid", "link"):
        val = entry.get(attr) if isinstance(entry, dict) else getattr(entry, attr, None)
        if val:
            return str(val).strip()
    return ""


def _entry_link(entry: Any) -> str | None:
    link = entry.get("link") if isinstance(entry, dict) else getattr(entry, "link", None)
    if isinstance(link, str) and link.startswith(("http://", "https://")):
        return link
    # Sometimes the link is in a `links` array of dicts.
    links = entry.get("links") if isinstance(entry, dict) else getattr(entry, "links", None)
    if links:
        for ln in links:
            href = ln.get("href") if isinstance(ln, dict) else getattr(ln, "href", None)
            if href and isinstance(href, str) and href.startswith(("http://", "https://")):
                return href
    return None


def capture_feed(
    settings: Settings,
    feed_url: str,
    *,
    limit: int = DEFAULT_LIMIT,
    extra_tags: list[str] | None = None,
    parser=feedparser.parse,
    capture_fn=None,
) -> FeedReport:
    """Walk a feed, capturing every new entry into the vault."""
    if capture_fn is None:
        capture_fn = capture_url

    parsed = parser(feed_url)
    if getattr(parsed, "bozo", False) and not getattr(parsed, "entries", []):
        # bozo with no entries means a hard parse failure; bozo with entries is fine.
        reason = str(getattr(parsed, "bozo_exception", "unknown parse error"))
        raise FeedError(f"Failed to parse feed {feed_url}: {reason}")

    entries = list(getattr(parsed, "entries", []))[:limit]
    feed_title = (
        (parsed.feed.get("title") if hasattr(parsed.feed, "get") else getattr(parsed.feed, "title", ""))
        or feed_url
    )

    state = _load_state(settings)
    feed_state = state.get(feed_url, {})
    seen_ids: set[str] = set(feed_state.get("seen_ids", []))

    tags = list(extra_tags or [])
    if "rss" not in tags:
        tags.append("rss")

    report = FeedReport(feed_url=feed_url, feed_title=feed_title, entries_total=len(entries))
    for entry in entries:
        link = _entry_link(entry)
        eid = _entry_id(entry) or (link or "")
        title = entry.get("title", "") if isinstance(entry, dict) else getattr(entry, "title", "")
        if not link:
            report.errors.append(
                FeedEntryResult(url="", title=title, status="error", error="No link in entry")
            )
            continue
        if eid in seen_ids:
            report.seen.append(FeedEntryResult(url=link, title=title, status="seen"))
            continue
        try:
            page: CapturedPage = capture_fn(settings, link, extra_tags=tags)
        except CaptureError as e:
            report.errors.append(
                FeedEntryResult(url=link, title=title, status="error", error=str(e))
            )
            continue
        report.captured.append(
            FeedEntryResult(
                url=link,
                title=page.title or title,
                status="captured",
                relpath=page.relpath,
            )
        )
        if eid:
            seen_ids.add(eid)

    # Persist trimmed state (keep at most 2× limit so the file doesn't grow forever).
    state[feed_url] = {"seen_ids": list(seen_ids)[-max(limit * 2, 100):], "feed_title": feed_title}
    _save_state(settings, state)
    return report
