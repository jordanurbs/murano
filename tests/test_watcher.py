"""Phase 2 — watcher event translation tests (watchfiles itself is mocked)."""

from __future__ import annotations

from pathlib import Path

from watchfiles import Change

from murano.vault.watcher import _events_to_subpaths, _is_markdown


def test_is_markdown_filter() -> None:
    assert _is_markdown("/x/y/note.md")
    assert _is_markdown("/x/y/Note.MARKDOWN")
    assert not _is_markdown("/x/y/.DS_Store")
    assert not _is_markdown("/x/y/note.txt")
    assert not _is_markdown("/x/y/note.md.bak")


def test_events_to_subpaths_relative_dedup(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "sub").mkdir()
    (vault / "a.md").write_text("a")
    (vault / "sub" / "b.md").write_text("b")

    events = [
        (Change.added, str(vault / "a.md")),
        (Change.modified, str(vault / "a.md")),
        (Change.added, str(vault / "sub" / "b.md")),
        (Change.added, str(vault / "ignored.txt")),
        (Change.added, "/totally/outside/vault/x.md"),
    ]
    subpaths = _events_to_subpaths(events, vault)
    assert subpaths == {Path("a.md"), Path("sub/b.md")}
