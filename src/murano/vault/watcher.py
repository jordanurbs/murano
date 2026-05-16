"""Real-time vault watcher.

Wraps `watchfiles` to translate filesystem events into per-file reindex calls.
Idempotency comes from the indexer's content-hash check, so we can safely
trigger an index on every notification without worrying about duplicate work.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from watchfiles import Change, watch

from ..config import Settings
from ..index.indexer import FileResult, index_vault

MARKDOWN_EXTS = (".md", ".markdown")


def _is_markdown(path: str) -> bool:
    return path.lower().endswith(MARKDOWN_EXTS)


def _events_to_subpaths(changes: Iterable[tuple[Change, str]], vault_root: Path) -> set[Path]:
    """Map filesystem events to a deduplicated set of vault-relative subpaths."""
    out: set[Path] = set()
    root = vault_root.resolve()
    for _change, abs_path in changes:
        if not _is_markdown(abs_path):
            continue
        p = Path(abs_path).resolve()
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        out.add(rel)
    return out


def watch_vault(
    settings: Settings,
    *,
    debounce_ms: int = 800,
    progress: Callable[[FileResult], None] | None = None,
    on_batch: Callable[[set[Path], Any], None] | None = None,
    stop_event: Any = None,
) -> None:
    """Watch the vault and reindex changed files until interrupted.

    Args:
        debounce_ms: passed straight to watchfiles; coalesces rapid edits.
        progress:    forwarded to index_vault() for per-file updates.
        on_batch:    optional callback called once per change batch with the set
                     of relpaths re-evaluated and the IndexReport.
        stop_event:  optional threading.Event-like (`.is_set()`); when set,
                     the watcher returns cleanly.
    """
    vault = settings.vault_root.resolve()
    vault.mkdir(parents=True, exist_ok=True)
    for changes in watch(
        str(vault),
        watch_filter=lambda _change, path: _is_markdown(path),
        debounce=debounce_ms,
        stop_event=stop_event,
    ):
        subpaths = _events_to_subpaths(changes, vault)
        if not subpaths:
            continue
        for rel in subpaths:
            report = index_vault(settings, subpath=rel, progress=progress)
            if on_batch:
                on_batch({rel}, report)
