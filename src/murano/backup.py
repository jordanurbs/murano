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
"""

from __future__ import annotations

import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

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


@dataclass
class BackupReport:
    out_path: Path
    file_count: int
    total_bytes: int
    included_config: bool
    included_usage_log: bool
    elapsed_seconds: float


def _iter_vault_files(vault_root: Path):
    """All Markdown files in the vault, hidden dirs skipped."""
    for child in sorted(vault_root.rglob("*")):
        if not child.is_file():
            continue
        if any(part.startswith(".") for part in child.relative_to(vault_root).parts):
            continue
        if not any(child.match(g) for g in VAULT_GLOBS):
            continue
        yield child


def export_vault(settings: Settings, out_path: Path) -> BackupReport:
    """Write a zip containing only the vault's Markdown files."""
    started = time.monotonic()
    out_path = out_path.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    total = 0
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for src in _iter_vault_files(settings.vault_root):
            rel = src.relative_to(settings.vault_root)
            zf.write(src, arcname=str(Path("vault") / rel))
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
            rel = src.relative_to(settings.vault_root)
            zf.write(src, arcname=str(Path("vault") / rel))
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
