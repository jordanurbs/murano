"""Vault export + backup.

Two related operations:
    - export(out_path):
        Just the vault contents (Markdown files). Useful if you want to move
        your notes to Obsidian or share them with another person. The derived
        index in ~/.murano/ is rebuildable, so we skip it.

    - backup(out_path):
        Vault + config.toml + the optional usage log. Skips chunks.db and
        summary_tree.db (rebuildable). NEVER includes the Venice API key —
        the key lives in the OS keychain and is not in any file we touch.

Both produce a single .zip file. We deliberately avoid tar.gz because zip is
universally readable on macOS, Windows, and Linux without extra tools.

Symlink policy (audit-fix): a Markdown symlink inside the vault that resolves
to a file *outside* the vault is silently skipped, NOT followed. Previously
zip.write() would dereference the symlink and copy the target's bytes into
the zip under the vault-relative name — the same class of escape the indexer
was hardened against.
"""

from __future__ import annotations

import logging
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .security import VaultPathError, relpath_in_vault

VAULT_GLOBS: tuple[str, ...] = ("*.md", "*.markdown")
NEVER_INCLUDE_NAMES = {
    "chunks.db",
    "chunks.db-journal",
    "chunks.db-wal",
    "chunks.db-shm",
    "summary_tree.db",
    "summary_tree.db-journal",
    "summary_tree.db-wal",
    "summary_tree.db-shm",
}

_logger = logging.getLogger("murano.backup")


@dataclass
class BackupReport:
    out_path: Path
    file_count: int
    total_bytes: int
    included_config: bool
    included_usage_log: bool
    elapsed_seconds: float


def _iter_vault_files(vault_root: Path):
    """All Markdown files in the vault, hidden dirs skipped.

    Symlinks that resolve outside the vault are dropped on the floor —
    we never want zip.write() to follow them and copy out-of-vault bytes
    into the backup under a vault-relative name. (Audit found this in
    round 3; same class as the round-1 indexer fix.)
    """
    vault_resolved = vault_root.resolve()
    for child in sorted(vault_root.rglob("*")):
        # Resolve THIS candidate and verify it's a real descendant of vault.
        try:
            resolved = child.resolve()
            rel = resolved.relative_to(vault_resolved)
        except (OSError, ValueError):
            _logger.debug("skipping out-of-vault path: %s", child)
            continue
        # Check is_file() on the *resolved* path so dangling symlinks die here.
        if not resolved.is_file():
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        if not any(resolved.match(g) for g in VAULT_GLOBS):
            continue
        yield resolved


def _safe_arcname(vault_root: Path, resolved_file: Path) -> str | None:
    """Return the vault-relative POSIX path for zip arcname, or None to skip."""
    try:
        return "vault/" + relpath_in_vault(vault_root, resolved_file).replace("\\", "/")
    except VaultPathError:
        return None


def export_vault(settings: Settings, out_path: Path) -> BackupReport:
    """Write a zip containing only the vault's Markdown files."""
    started = time.monotonic()
    out_path = out_path.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    total = 0
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for src in _iter_vault_files(settings.vault_root):
            arcname = _safe_arcname(settings.vault_root, src)
            if arcname is None:  # symlink escaped during the race; skip
                continue
            zf.write(src, arcname=arcname)
            n += 1
            total += src.stat().st_size
    return BackupReport(
        out_path=out_path,
        file_count=n,
        total_bytes=total,
        included_config=False,
        included_usage_log=False,
        elapsed_seconds=time.monotonic() - started,
    )


def backup(settings: Settings, out_path: Path, include_usage: bool = True) -> BackupReport:
    """Write a zip with the vault + config.toml + (optionally) the usage log.

    Skips the rebuildable derived index. Never includes the Venice API key.
    """
    started = time.monotonic()
    out_path = out_path.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    total = 0
    included_config = False
    included_usage = False
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for src in _iter_vault_files(settings.vault_root):
            arcname = _safe_arcname(settings.vault_root, src)
            if arcname is None:
                continue
            zf.write(src, arcname=arcname)
            n += 1
            total += src.stat().st_size

        if settings.config_path.exists():
            zf.write(settings.config_path, arcname="murano/config.toml")
            included_config = True
            n += 1
            total += settings.config_path.stat().st_size

        if include_usage:
            usage_path = settings.logs_dir / "usage.jsonl"
            if usage_path.exists():
                zf.write(usage_path, arcname="murano/logs/usage.jsonl")
                included_usage = True
                n += 1
                total += usage_path.stat().st_size

        # Defensive: explicitly assert we never include the DBs even if some
        # future caller reuses this function with a different glob set.
        for name in zf.namelist():
            if Path(name).name in NEVER_INCLUDE_NAMES:
                raise RuntimeError(
                    f"backup integrity check failed: {name!r} was included"
                )

    return BackupReport(
        out_path=out_path,
        file_count=n,
        total_bytes=total,
        included_config=included_config,
        included_usage_log=included_usage,
        elapsed_seconds=time.monotonic() - started,
    )


def default_export_path(settings: Settings, prefix: str) -> Path:  # noqa: ARG001
    """`murano-{prefix}-YYYYMMDD-HHMMSS.zip` in the current working dir."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Path.cwd() / f"murano-{prefix}-{stamp}.zip"
