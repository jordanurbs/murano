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
from ..security import UnsafeURLError, assert_public_http_url
from .web import CapturedPage, CaptureError, capture_url

DEFAULT_LIMIT = 20
# Upper bound on how many feed entries we'll *consider* per run. Beyond this,
# we stop scanning even if `limit` hasn't been reached — protects against
# pathological feeds with thousands of items.
MAX_ENTRIES_TO_SCAN = 500
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

    # Audit-4: validate the FEED URL before fetching, then re-validate every
    # entry link the publisher advertises. The user typed the feed URL; the
    # publisher chose the entry links. Without this, a malicious or
    # compromised feed could point at http://127.0.0.1:3000/api/v1/index,
    # http://192.168.1.1/router/admin, http://169.254.169.254/, etc.
    try:
        assert_public_http_url(feed_url)
    except UnsafeURLError as e:
        raise FeedError(str(e)) from e

    parsed = parser(feed_url)
    if getattr(parsed, "bozo", False) and not getattr(parsed, "entries", []):
        # bozo with no entries means a hard parse failure; bozo with entries is fine.
        reason = str(getattr(parsed, "bozo_exception", "unknown parse error"))
        raise FeedError(f"Failed to parse feed {feed_url}: {reason}")

    # IMPORTANT: do NOT slice entries by `limit` here. The audit found that
    # `entries[:limit]` blocks later entries when an earlier entry is
    # permanently broken — with `--limit 1`, the same failing entry gets
    # retried every run and the rest of the feed is unreachable. Instead,
    # we walk ALL entries (capped by a hard MAX_ENTRIES_TO_SCAN below) and
    # stop after attempting `limit` *new* (not-yet-seen) ones.
    all_entries = list(getattr(parsed, "entries", []))
    feed_title = (
        (parsed.feed.get("title") if hasattr(parsed.feed, "get") else getattr(parsed.feed, "title", ""))
        or feed_url
    )

    state = _load_state(settings)
    feed_state = state.get(feed_url, {})
    # `seen_ids` is persisted as an *ordered* list (oldest -> newest). We keep
    # an in-memory parallel set for O(1) membership lookup. Don't switch to a
    # bare set: set iteration order is non-deterministic and the eventual
    # FIFO trim below would silently drop arbitrary IDs, causing duplicate
    # captures on subsequent runs. (Caught by the second-round audit.)
    raw_seen = feed_state.get("seen_ids", [])
    # Defensive: if a legacy state file persisted a non-list, coerce to list.
    seen_list: list[str] = list(raw_seen) if isinstance(raw_seen, list) else list(raw_seen)
    seen_set: set[str] = set(seen_list)

    tags = list(extra_tags or [])
    if "rss" not in tags:
        tags.append("rss")

    report = FeedReport(feed_url=feed_url, feed_title=feed_title, entries_total=len(all_entries))
    attempts = 0  # number of NEW (not-yet-seen) entries we've tried to capture
    for entry in all_entries[:MAX_ENTRIES_TO_SCAN]:
        if attempts >= limit:
            break
        link = _entry_link(entry)
        eid = _entry_id(entry) or (link or "")
        title = entry.get("title", "") if isinstance(entry, dict) else getattr(entry, "title", "")
        if not link:
            report.errors.append(
                FeedEntryResult(url="", title=title, status="error", error="No link in entry")
            )
            # Linkless entries don't count against `limit` — they're malformed,
            # and counting them would let a feed publisher starve us by
            # putting `limit` linkless entries at the top.
            continue
        # Re-validate each entry link's host before passing to capture_url.
        # capture_url itself also validates, but doing it here too gives a
        # clearer per-entry error in the report instead of a generic
        # CaptureError after capture_url's pre-flight.
        try:
            assert_public_http_url(link)
        except UnsafeURLError as e:
            report.errors.append(
                FeedEntryResult(url=link, title=title, status="error", error=str(e))
            )
            # SSRF-blocked links don't burn the limit budget either; they're
            # malicious / misconfigured feed content, not real attempts.
            continue
        if eid in seen_set:
            report.seen.append(FeedEntryResult(url=link, title=title, status="seen"))
            # Already-seen entries don't count against `limit` either; we want
            # `limit` to mean "try `limit` *new* things this run".
            continue

        # This is a NEW entry — we will attempt it whether it succeeds or fails.
        attempts += 1
        try:
            page: CapturedPage = capture_fn(settings, link, extra_tags=tags)
        except CaptureError as e:
            report.errors.append(
                FeedEntryResult(url=link, title=title, status="error", error=str(e))
            )
            # NOTE: we do NOT add `eid` to seen_set on failure. A transient
            # network error should be retried next run. Persistently-broken
            # entries will keep failing, but each run also moves on to other
            # entries (because `attempts` is incremented above) so they no
            # longer starve the feed.
            continue
        report.captured.append(
            FeedEntryResult(
                url=link,
                title=page.title or title,
                status="captured",
                relpath=page.relpath,
            )
        )
        if eid and eid not in seen_set:
            seen_list.append(eid)
            seen_set.add(eid)

    # Persist trimmed state (keep at most 2× limit so the file doesn't grow
    # forever). FIFO: drop the oldest, keep the newest. Deterministic across
    # runs and Python builds because `seen_list` preserves insertion order.
    trim_window = max(limit * 2, 100)
    state[feed_url] = {
        "seen_ids": seen_list[-trim_window:],
        "feed_title": feed_title,
    }
    _save_state(settings, state)
    return report
