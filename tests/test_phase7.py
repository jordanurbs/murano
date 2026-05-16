"""Phase 7 — tests for usage tracker, export/backup, licenses, RSS capture."""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pytest

from murano import backup as backup_mod
from murano import licenses as licenses_mod
from murano import usage as usage_mod
from murano.capture import feed as feed_mod
from murano.capture.web import CapturedPage
from murano.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    vault.mkdir()
    data.mkdir()
    s = Settings(vault_root=vault, data_root=data)
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    return s


# ---------- usage tracker ----------


def test_log_usage_appends_jsonl(settings: Settings) -> None:
    usage_mod.log_usage(
        settings.data_root,
        usage_mod.UsageEvent(
            operation="chat",
            model="qwen-3-6-plus",
            prompt_tokens=120,
            completion_tokens=42,
            total_tokens=162,
        ),
    )
    usage_mod.log_usage(
        settings.data_root,
        usage_mod.UsageEvent(
            operation="embed",
            model="text-embedding-qwen3-8b",
            prompt_tokens=50,
            total_tokens=50,
        ),
    )
    path = settings.logs_dir / "usage.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_iter_usage_skips_malformed_lines(settings: Settings) -> None:
    path = settings.logs_dir / "usage.jsonl"
    path.write_text(
        '{"operation":"chat","model":"x","prompt_tokens":1,"completion_tokens":2,"total_tokens":3,"timestamp":1700000000}\n'
        "not json at all\n"
        '{"bad_key": true}\n'
        '{"operation":"embed","model":"y","prompt_tokens":10,"total_tokens":10,"timestamp":1700001000}\n',
        encoding="utf-8",
    )
    events = list(usage_mod.iter_usage(settings.data_root))
    assert len(events) == 3  # the empty-fields one becomes a valid event with defaults
    ops = [e.operation for e in events]
    assert "chat" in ops and "embed" in ops


def test_summarize_buckets_by_op_model_and_day(settings: Settings) -> None:
    events = [
        usage_mod.UsageEvent(
            operation="chat", model="A",
            prompt_tokens=100, completion_tokens=50, total_tokens=150,
            timestamp=time.mktime((2026, 5, 16, 12, 0, 0, 0, 0, 0)),
        ),
        usage_mod.UsageEvent(
            operation="chat", model="A",
            prompt_tokens=200, completion_tokens=80, total_tokens=280,
            timestamp=time.mktime((2026, 5, 16, 13, 0, 0, 0, 0, 0)),
        ),
        usage_mod.UsageEvent(
            operation="embed", model="B",
            prompt_tokens=40, completion_tokens=0, total_tokens=40,
            timestamp=time.mktime((2026, 5, 17, 9, 0, 0, 0, 0, 0)),
        ),
    ]
    s = usage_mod.summarize(events)
    assert s.total_events == 3
    assert s.total_tokens == 470
    assert s.by_operation["chat"]["total_tokens"] == 430
    assert s.by_operation["embed"]["total_tokens"] == 40
    assert s.by_model["A"]["events"] == 2
    assert s.by_day["2026-05-16"]["total_tokens"] == 430
    assert s.by_day["2026-05-17"]["total_tokens"] == 40


def test_extract_usage_handles_missing_field() -> None:
    class _NoUsage:
        usage = None

    assert usage_mod.extract_usage_from_response(_NoUsage()) == (0, 0, 0)

    class _Usage:
        prompt_tokens = 10
        completion_tokens = 5
        total_tokens = 15

    class _Resp:
        usage = _Usage()

    assert usage_mod.extract_usage_from_response(_Resp()) == (10, 5, 15)


# ---------- export / backup ----------


def _seed_vault(settings: Settings) -> None:
    (settings.vault_root / "alpha.md").write_text("# Alpha\n\nhello\n")
    sub = settings.vault_root / "cooking"
    sub.mkdir()
    (sub / "risotto.md").write_text("# Risotto\n\ncreamy\n")
    (settings.vault_root / "skipme.txt").write_text("not markdown")
    hidden = settings.vault_root / ".hidden"
    hidden.mkdir()
    (hidden / "ghost.md").write_text("# Ghost\n\nshould be skipped\n")


def test_export_vault_only_includes_markdown(settings: Settings, tmp_path: Path) -> None:
    _seed_vault(settings)
    out = tmp_path / "export.zip"
    report = backup_mod.export_vault(settings, out)
    assert out.exists()
    assert report.file_count == 2  # alpha.md + cooking/risotto.md (no .txt, no .hidden)
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert "vault/alpha.md" in names
    assert "vault/cooking/risotto.md" in names
    assert all("skipme" not in n for n in names)
    assert all(".hidden" not in n for n in names)


def test_backup_includes_config_and_usage_but_never_dbs(
    settings: Settings, tmp_path: Path
) -> None:
    _seed_vault(settings)
    settings.config_path.write_text('vault_root = "x"\n', encoding="utf-8")
    (settings.logs_dir / "usage.jsonl").write_text(
        '{"operation":"chat","model":"x"}\n', encoding="utf-8"
    )
    # Drop fake DBs into the data root and make sure they DON'T end up in the zip.
    settings.chunks_db.write_bytes(b"fake sqlite")
    settings.summary_tree_db.write_bytes(b"fake sqlite")

    out = tmp_path / "backup.zip"
    report = backup_mod.backup(settings, out)
    assert report.included_config is True
    assert report.included_usage_log is True

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert "murano/config.toml" in names
    assert "murano/logs/usage.jsonl" in names
    assert "vault/alpha.md" in names
    assert "vault/cooking/risotto.md" in names
    assert all("chunks.db" not in n for n in names)
    assert all("summary_tree.db" not in n for n in names)


def test_backup_skips_usage_when_disabled(settings: Settings, tmp_path: Path) -> None:
    _seed_vault(settings)
    (settings.logs_dir / "usage.jsonl").write_text('{}\n', encoding="utf-8")
    out = tmp_path / "backup.zip"
    report = backup_mod.backup(settings, out, include_usage=False)
    assert report.included_usage_log is False
    with zipfile.ZipFile(out) as zf:
        assert "murano/logs/usage.jsonl" not in zf.namelist()


# ---------- licenses ----------


def test_classify_license_text_detects_copyleft() -> None:
    assert licenses_mod._classify_license_text("GPL-3.0")[0] is True
    assert licenses_mod._classify_license_text("Affero GPL")[0] is True
    assert licenses_mod._classify_license_text("AGPL v3")[0] is True
    assert licenses_mod._classify_license_text("LGPL-2.1")[0] is True
    assert licenses_mod._classify_license_text("MIT License") == (False, None)
    assert licenses_mod._classify_license_text("Apache-2.0") == (False, None)
    assert licenses_mod._classify_license_text("BSD-3-Clause") == (False, None)


def test_classify_license_text_accepts_or_expressions() -> None:
    """Multi-licensed packages are permissive as long as one alternative is permissive."""
    # tld's real license; user can pick MPL.
    assert licenses_mod._classify_license_text(
        "MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later"
    ) == (False, None)
    # All alternatives copyleft -> flagged.
    assert licenses_mod._classify_license_text("GPL-2.0 OR AGPL-3.0")[0] is True
    # Empty string is not copyleft.
    assert licenses_mod._classify_license_text("") == (False, None)


def test_audit_returns_nonempty_list() -> None:
    pkgs = licenses_mod.audit()
    assert pkgs, "expected at least some installed packages"
    names = {p.name.lower() for p in pkgs}
    # We know these are installed in our env.
    assert "fastapi" in names or "openai" in names


def test_no_copyleft_in_current_install() -> None:
    """Smoke check: Murano's own install tree must have zero copyleft deps."""
    pkgs = licenses_mod.audit()
    bad = licenses_mod.copyleft_packages(pkgs)
    assert bad == [], (
        "Copyleft packages crept into the install: "
        + ", ".join(f"{p.name} ({p.license})" for p in bad)
    )


# ---------- RSS feed capture ----------


class _FakeEntry:
    def __init__(self, link: str, title: str, eid: str | None = None) -> None:
        self.link = link
        self.title = title
        self.id = eid or link

    def get(self, key, default=None):  # mimic dict.get for compatibility
        return getattr(self, key, default)


class _FakeFeed:
    def __init__(self, title: str) -> None:
        self.title = title

    def get(self, key, default=None):
        return getattr(self, key, default)


class _FakeParsed:
    def __init__(self, entries: list, feed_title: str, bozo: bool = False) -> None:
        self.entries = entries
        self.feed = _FakeFeed(feed_title)
        self.bozo = bozo
        self.bozo_exception = "broken" if bozo else None


def test_capture_feed_walks_entries_and_records_state(
    settings: Settings, tmp_path: Path
) -> None:
    parsed = _FakeParsed(
        entries=[
            _FakeEntry("https://example.com/a", "Article A", "id-a"),
            _FakeEntry("https://example.com/b", "Article B", "id-b"),
        ],
        feed_title="Example Feed",
    )

    captured_calls = []

    def fake_capture(_s, url, *, extra_tags=None):
        captured_calls.append((url, extra_tags))
        relpath = f"web-captures/2026-05-16-{url.rsplit('/', 1)[-1]}.md"
        return CapturedPage(
            url=url,
            title=f"Captured {url}",
            relpath=relpath,
            absolute_path=tmp_path / relpath,
            word_count=100,
            byte_count=500,
            site_name=None,
            published_date=None,
        )

    report = feed_mod.capture_feed(
        settings,
        "https://example.com/feed.xml",
        limit=10,
        parser=lambda _url: parsed,
        capture_fn=fake_capture,
    )

    assert report.entries_total == 2
    assert len(report.captured) == 2
    assert len(report.seen) == 0
    assert len(captured_calls) == 2
    assert any("rss" in (tags or []) for _, tags in captured_calls)

    # Second run should skip both as already-seen.
    report2 = feed_mod.capture_feed(
        settings,
        "https://example.com/feed.xml",
        limit=10,
        parser=lambda _url: parsed,
        capture_fn=fake_capture,
    )
    assert len(report2.captured) == 0
    assert len(report2.seen) == 2


def test_capture_feed_raises_on_hard_parse_failure(settings: Settings) -> None:
    parsed = _FakeParsed(entries=[], feed_title="", bozo=True)
    with pytest.raises(feed_mod.FeedError, match="Failed to parse"):
        feed_mod.capture_feed(
            settings,
            "https://example.com/bad.xml",
            parser=lambda _url: parsed,
            capture_fn=lambda *a, **k: None,
        )


def test_capture_feed_ignores_entries_without_link(settings: Settings) -> None:
    class _LinklessEntry:
        title = "No link here"

        def get(self, key, default=None):
            return getattr(self, key, default)

    parsed = _FakeParsed(entries=[_LinklessEntry()], feed_title="Test")
    report = feed_mod.capture_feed(
        settings,
        "https://example.com/feed.xml",
        parser=lambda _url: parsed,
        capture_fn=lambda *a, **k: None,
    )
    assert len(report.errors) == 1
    assert "No link" in (report.errors[0].error or "")


def test_capture_feed_seen_ids_trim_is_deterministic_fifo(
    settings: Settings, tmp_path: Path
) -> None:
    """Regression for the audit-found set-ordering bug.

    Previously `seen_ids` was a set and persisted via `list(set)[-N:]`;
    set iteration order is non-deterministic in Python, so once the set
    grew beyond the trim window, *which* IDs survived was arbitrary —
    causing duplicate captures on subsequent runs.

    The fix uses an ordered list backed by a set for lookups; the FIFO
    trim must keep the newest N entries.
    """
    feed_url = "https://example.com/feed.xml"

    # Pre-seed the state with 200 sequential IDs from a prior run, so a
    # subsequent capture run with limit=50 (trim window = 100) will be
    # forced to trim 150 of them deterministically.
    state_path = settings.logs_dir / feed_mod.STATE_FILENAME
    state_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    pre_seeded = [f"id-{i:04d}" for i in range(200)]
    state_path.write_text(
        json.dumps({feed_url: {"seen_ids": pre_seeded, "feed_title": "Big feed"}}),
        encoding="utf-8",
    )

    def fake_capture(_s, url, *, extra_tags=None):  # noqa: ARG001
        return CapturedPage(
            url=url, title="T",
            relpath=f"web-captures/{url.rsplit('/', 1)[-1]}.md",
            absolute_path=tmp_path / "x.md",
            word_count=1, byte_count=1,
            site_name=None, published_date=None,
        )

    # New entries id-1000..id-1049 (none seen before). limit=50, window=100.
    parsed = _FakeParsed(
        entries=[
            _FakeEntry(f"https://example.com/{i}", f"T{i}", f"id-{i:04d}")
            for i in range(1000, 1050)
        ],
        feed_title="Big feed",
    )
    report = feed_mod.capture_feed(
        settings, feed_url, limit=50,
        parser=lambda _url: parsed, capture_fn=fake_capture,
    )
    assert len(report.captured) == 50

    state = feed_mod._load_state(settings)
    seen = state[feed_url]["seen_ids"]
    # Total before trim: 200 (pre-seeded) + 50 (new) = 250.
    # Trim window: max(50 * 2, 100) = 100. So keep the newest 100.
    # That means id-0150..id-0199 (last 50 of pre-seed) + id-1000..id-1049.
    assert isinstance(seen, list)
    assert len(seen) == 100, f"expected window=100, got {len(seen)}"
    assert seen[0] == "id-0150", f"oldest survivor wrong; got {seen[0]}"
    assert seen[-1] == "id-1049", f"newest survivor wrong; got {seen[-1]}"

    # And critically: running this test multiple times must produce the
    # *same* survivors. Re-run with no new entries; trim is a no-op.
    parsed_empty = _FakeParsed(entries=[], feed_title="Big feed")
    feed_mod.capture_feed(
        settings, feed_url, limit=50,
        parser=lambda _url: parsed_empty, capture_fn=fake_capture,
    )
    state2 = feed_mod._load_state(settings)
    assert state2[feed_url]["seen_ids"] == seen, "trim must be deterministic"


def test_capture_feed_legacy_set_state_is_coerced_to_list(
    settings: Settings, tmp_path: Path
) -> None:
    """Defensive: pre-fix state files stored seen_ids as a list-derived-from-set.
    A future run must still treat it as ordered without crashing."""
    # Pre-seed state file with a list-typed (post-fix) shape.
    state_path = settings.logs_dir / feed_mod.STATE_FILENAME
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        '{"https://x/feed": {"seen_ids": ["a", "b"], "feed_title": "X"}}',
        encoding="utf-8",
    )
    parsed = _FakeParsed(
        entries=[_FakeEntry("https://example.com/c", "C", "c")], feed_title="X"
    )
    report = feed_mod.capture_feed(
        settings,
        "https://x/feed",
        parser=lambda _url: parsed,
        capture_fn=lambda *a, **k: CapturedPage(
            url="x", title="x", relpath="r.md",
            absolute_path=tmp_path / "x.md", word_count=1, byte_count=1,
            site_name=None, published_date=None,
        ),
    )
    assert len(report.captured) == 1
    state = feed_mod._load_state(settings)
    assert "c" in state["https://x/feed"]["seen_ids"]
    assert state["https://x/feed"]["seen_ids"][-1] == "c"
