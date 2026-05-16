"""Security helpers shared by every code path that takes a user-supplied path.

Why this module exists: prior to the v1 audit, three call sites
(`/api/v1/open`, `/api/v1/vault/file`, `/file` UI) used the pattern

    candidate = (vault_root / user_path).resolve()
    if not str(candidate).startswith(str(vault_root.resolve())):
        raise ...

which is famously vulnerable to a sibling-prefix attack: a vault at
`/home/u/murano/vault` lets the attacker read `/home/u/murano/vault2/x.md`
because `"/home/u/murano/vault2/x.md".startswith("/home/u/murano/vault")`
returns True. Use `safe_vault_path` instead — it leans on Python's
`Path.relative_to` which enforces real directory boundaries.

This module also wraps `relative_to` to handle macOS's `/var` -> `/private/var`
symlink trip, by resolving both sides before comparing.
"""

from __future__ import annotations

from pathlib import Path


class VaultPathError(ValueError):
    """Raised when a user-supplied path escapes the vault root."""


def safe_vault_path(vault_root: Path, user_path: str | Path) -> Path:
    """Resolve `user_path` against `vault_root` and assert containment.

    Returns the absolute, resolved Path on success. Raises VaultPathError
    if the resolved candidate is not a strict descendant of (or equal to)
    the resolved vault root.

    The check is symbolic: existence of the target is NOT verified here.
    Caller decides whether to additionally check existence after success.
    """
    if user_path is None:
        raise VaultPathError("path is required")
    user_path_str = str(user_path)
    if not user_path_str.strip():
        raise VaultPathError("path must not be empty")

    vault_resolved = vault_root.resolve()
    candidate = (vault_resolved / user_path_str).resolve()

    # Path.relative_to raises ValueError when candidate is not under vault_resolved.
    try:
        candidate.relative_to(vault_resolved)
    except ValueError as e:
        raise VaultPathError(
            f"Path {user_path_str!r} resolves outside the vault."
        ) from e

    return candidate


def relpath_in_vault(vault_root: Path, candidate: Path) -> str:
    """Return `candidate` as a vault-relative POSIX-ish string.

    Raises VaultPathError if `candidate` is not inside the resolved vault.
    Unlike the previous best-effort helper, this NEVER silently falls back
    to an absolute path — we don't want absolute paths leaking into chunk
    records or citation keys.
    """
    vault_resolved = vault_root.resolve()
    candidate_resolved = candidate.resolve()
    try:
        return str(candidate_resolved.relative_to(vault_resolved))
    except ValueError as e:
        raise VaultPathError(
            f"Path {candidate} is not inside vault {vault_root}."
        ) from e
