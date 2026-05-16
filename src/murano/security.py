"""Security helpers shared by every code path that takes user-supplied input.

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

Audit-4 added URL host validation (`assert_public_http_url`) so the capture
path cannot be turned into an SSRF gadget pointing at loopback, link-local
metadata services, RFC-1918 networks, etc.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urlparse


class VaultPathError(ValueError):
    """Raised when a user-supplied path escapes the vault root."""


class UnsafeURLError(ValueError):
    """Raised when a user-supplied URL would target a non-public network."""


# Names that don't resolve at all on a stock system but still want to be
# blocked at the lexical layer — they're either localhost aliases or
# common internal-only TLDs that an attacker might use.
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
}

# Override env var. Setting this to "1" allows capturing URLs that would
# otherwise be rejected as non-public. Intended ONLY for dev environments
# (e.g. capturing http://localhost:8000/...). Off by default.
ALLOW_PRIVATE_CAPTURE_ENV = "MURANO_ALLOW_PRIVATE_CAPTURES"


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


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """An IP we're willing to let `capture` reach. Public-internet only."""
    # Reject every special-purpose range we can think of. The Python stdlib
    # `is_private` covers RFC-1918 / unique-local IPv6 / etc., but does NOT
    # cover loopback, link-local, multicast, reserved, unspecified — so we
    # check each one explicitly.
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    # IPv4-mapped IPv6 like `::ffff:127.0.0.1` is sneaky: the v6 address
    # itself doesn't claim is_loopback, but the embedded v4 might.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _ip_is_public(ip.ipv4_mapped)
    return True


def assert_public_http_url(
    url: str,
    *,
    allow_override: bool = True,
    resolver=socket.getaddrinfo,
) -> None:
    """Refuse `url` if it targets a non-public host.

    Raises UnsafeURLError on any of:
        - Non-http(s) scheme
        - No hostname
        - Hostname literally matches a blocked alias (localhost, etc.)
        - Hostname resolves (via DNS) to a non-public IP
        - Any single resolved address is non-public — we refuse the whole
          request rather than gamble on which address httpx picks

    `allow_override` honors the MURANO_ALLOW_PRIVATE_CAPTURES env var for
    dev workflows that legitimately want to capture localhost. Production
    callers (HTTP API, MCP tool, RSS feed walker) should pass
    `allow_override=False` so an attacker-controlled env var can't unlock
    the gadget for them.

    `resolver` is injectable for tests.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Refusing URL {url!r}: scheme must be http or https.")
    hostname = (parsed.hostname or "").strip()
    if not hostname:
        raise UnsafeURLError(f"Refusing URL {url!r}: missing hostname.")

    if allow_override and os.environ.get(ALLOW_PRIVATE_CAPTURE_ENV) == "1":
        return  # explicit dev opt-out

    if hostname.lower() in _BLOCKED_HOSTNAMES:
        raise UnsafeURLError(
            f"Refusing URL {url!r}: hostname {hostname!r} is a localhost alias."
        )

    # If the hostname IS an IP literal, validate it directly without DNS.
    try:
        ip = ipaddress.ip_address(hostname.strip("[]"))
        if not _ip_is_public(ip):
            raise UnsafeURLError(
                f"Refusing URL {url!r}: address {ip} is not on the public internet."
            )
        return
    except ValueError:
        pass  # not an IP literal; fall through to DNS resolution

    try:
        infos = resolver(hostname, None)
    except (socket.gaierror, OSError) as e:
        raise UnsafeURLError(
            f"Refusing URL {url!r}: DNS resolution failed ({e})."
        ) from e

    if not infos:
        raise UnsafeURLError(f"Refusing URL {url!r}: hostname resolved to no addresses.")

    for info in infos:
        addr = info[4][0]
        # Strip IPv6 zone identifier (`fe80::1%en0` -> `fe80::1`).
        addr = addr.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue  # weird thing the resolver returned; treat as suspect
        if not _ip_is_public(ip):
            raise UnsafeURLError(
                f"Refusing URL {url!r}: hostname {hostname!r} resolves to "
                f"non-public address {ip}."
            )


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
