"""Security regression tests covering the audit-found issues."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from murano.config import Settings
from murano.index.indexer import _relpath, iter_vault_files
from murano.security import VaultPathError, relpath_in_vault, safe_vault_path
from murano.venice import (
    CANONICAL_VENICE_HOST,
    LOCAL_API_KEY_ENV,
    VeniceAuthError,
    _is_canonical_venice,
    resolve_api_key,
)

# --- safe_vault_path / relpath_in_vault ------------------------------------


def test_safe_vault_path_accepts_child(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("x")
    p = safe_vault_path(vault, "a.md")
    assert p == (vault / "a.md").resolve()


def test_safe_vault_path_accepts_nested_child(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "sub").mkdir(parents=True)
    (vault / "sub" / "b.md").write_text("x")
    p = safe_vault_path(vault, "sub/b.md")
    assert p == (vault / "sub" / "b.md").resolve()


def test_safe_vault_path_rejects_traversal(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(VaultPathError):
        safe_vault_path(vault, "../../../etc/passwd")


def test_safe_vault_path_rejects_sibling_prefix(tmp_path: Path) -> None:
    """The exact bug class found by the audit: vault vs vault2."""
    vault = tmp_path / "vault"
    sibling = tmp_path / "vault2"
    vault.mkdir()
    sibling.mkdir()
    (sibling / "secret.md").write_text("nope")
    with pytest.raises(VaultPathError):
        safe_vault_path(vault, "../vault2/secret.md")


def test_safe_vault_path_rejects_absolute_outside(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(VaultPathError):
        safe_vault_path(vault, "/etc/passwd")


def test_safe_vault_path_rejects_empty(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(VaultPathError):
        safe_vault_path(vault, "")
    with pytest.raises(VaultPathError):
        safe_vault_path(vault, "   ")


def test_relpath_in_vault_raises_on_outside(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    elsewhere = tmp_path / "elsewhere"
    vault.mkdir()
    elsewhere.mkdir()
    secret = elsewhere / "x.md"
    secret.write_text("outside")
    with pytest.raises(VaultPathError):
        relpath_in_vault(vault, secret)


def test_relpath_in_vault_happy_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "sub").mkdir(parents=True)
    f = vault / "sub" / "x.md"
    f.write_text("x")
    assert relpath_in_vault(vault, f) == os.path.join("sub", "x.md")


# --- _relpath now raises instead of silently returning absolute paths ------


def test_relpath_in_indexer_raises_outside_vault(tmp_path: Path) -> None:
    """The audit flagged that _relpath fell back to absolute on out-of-vault
    paths, which would leak abspaths into chunk records. Now it raises."""
    tmp_path = tmp_path.resolve()  # macOS /var → /private/var stability
    vault = tmp_path / "vault"
    elsewhere = tmp_path / "elsewhere"
    vault.mkdir()
    elsewhere.mkdir()
    secret = elsewhere / "secret.md"
    secret.write_text("# Secret")
    with pytest.raises(VaultPathError):
        _relpath(vault, secret)


def test_iter_vault_files_skips_symlinked_files_pointing_outside(
    tmp_path: Path,
) -> None:
    """If a user (or attacker) drops a symlink in the vault pointing at a
    file outside, the walker must not yield the resolved out-of-vault file."""
    tmp_path = tmp_path.resolve()
    vault = tmp_path / "vault"
    elsewhere = tmp_path / "elsewhere"
    vault.mkdir()
    elsewhere.mkdir()
    real = vault / "real.md"
    real.write_text("# real")
    outside = elsewhere / "secret.md"
    outside.write_text("# secret")
    # File-level symlink inside the vault pointing at an outside file.
    (vault / "leaked.md").symlink_to(outside)

    files = list(iter_vault_files(vault))
    # Walker should yield real.md but not the resolved outside-vault target.
    paths = [str(p) for p in files]
    assert any("real.md" in p for p in paths)
    assert all("elsewhere" not in p for p in paths), paths


def test_iter_vault_files_rejects_subpath_pointing_outside(tmp_path: Path) -> None:
    tmp_path = tmp_path.resolve()
    vault = tmp_path / "vault"
    other = tmp_path / "other"
    vault.mkdir()
    other.mkdir()
    (other / "x.md").write_text("nope")
    # subpath="../other" must produce zero results.
    files = list(iter_vault_files(vault, Path("../other")))
    assert files == []


# --- keychain key gating by base URL ---------------------------------------


def test_is_canonical_venice_matches_only_official_host() -> None:
    """Audit-4: must require HTTPS for the canonical match. A downgraded URL
    like `http://api.venice.ai/...` is *not* canonical because the keychain
    key would otherwise be sent in cleartext."""
    assert _is_canonical_venice("https://api.venice.ai/api/v1") is True
    assert _is_canonical_venice("https://api.venice.ai") is True
    assert _is_canonical_venice("https://API.VENICE.AI/api/v1") is True
    # Plaintext downgrade is no longer canonical — audit-4 fix.
    assert _is_canonical_venice("http://api.venice.ai/api/v1") is False
    assert _is_canonical_venice("https://api.venice.ai.evil.com/api/v1") is False
    assert _is_canonical_venice("https://evil.com/api.venice.ai/v1") is False
    assert _is_canonical_venice("http://localhost:11434/v1") is False


def test_downgraded_canonical_url_does_not_leak_keychain_key() -> None:
    """Concrete regression for the audit-4 scheme-downgrade attack:
    `MURANO_VENICE_BASE_URL=http://api.venice.ai/api/v1` must NOT cause
    resolve_api_key() to return the keychain Venice key."""
    from unittest.mock import patch

    from murano.config import Settings
    from murano.venice import resolve_api_key

    s = Settings(venice_base_url="http://api.venice.ai/api/v1")
    with patch("murano.venice.get_api_key", return_value="sk-VENICE-SECRET") as gk:
        # No MURANO_API_KEY set -> placeholder ("no-auth"), NOT the keychain key.
        key = resolve_api_key(s)
    assert key != "sk-VENICE-SECRET"
    gk.assert_not_called()


def test_resolve_api_key_uses_keychain_for_canonical_venice() -> None:
    s = Settings(venice_base_url="https://api.venice.ai/api/v1")
    with patch("murano.venice.get_api_key", return_value="sk-fake-key"):
        assert resolve_api_key(s) == "sk-fake-key"


def test_resolve_api_key_raises_when_venice_key_missing() -> None:
    s = Settings(venice_base_url="https://api.venice.ai/api/v1")
    with patch("murano.venice.get_api_key", return_value=None), pytest.raises(VeniceAuthError):
        resolve_api_key(s)


def test_resolve_api_key_never_sends_keychain_to_custom_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact audit finding: a tampered MURANO_VENICE_BASE_URL must not
    cause the Venice keychain key to be sent to an arbitrary host."""
    monkeypatch.delenv(LOCAL_API_KEY_ENV, raising=False)
    s = Settings(venice_base_url="http://attacker.example/api/v1")
    with patch("murano.venice.get_api_key", return_value="sk-VENICE-SECRET") as gk:
        key = resolve_api_key(s)
    # Keychain key never returned for non-canonical hosts.
    assert key != "sk-VENICE-SECRET"
    # And we never even consulted the keychain on the non-canonical path.
    gk.assert_not_called()


def test_resolve_api_key_uses_env_for_custom_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOCAL_API_KEY_ENV, "local-token")
    s = Settings(venice_base_url="http://localhost:11434/v1")
    with patch("murano.venice.get_api_key", return_value="sk-VENICE") as gk:
        assert resolve_api_key(s) == "local-token"
    gk.assert_not_called()


def test_canonical_host_constant_is_correct() -> None:
    assert CANONICAL_VENICE_HOST == "api.venice.ai"


# --- SSRF guard (assert_public_http_url) -----------------------------------


def test_assert_public_http_url_rejects_loopback_literals() -> None:
    from murano.security import UnsafeURLError, assert_public_http_url

    for u in (
        "http://127.0.0.1/",
        "http://127.1.2.3/",
        "http://[::1]/",
        "http://0.0.0.0/",
    ):
        with pytest.raises(UnsafeURLError):
            assert_public_http_url(u)


def test_assert_public_http_url_rejects_private_ranges() -> None:
    from murano.security import UnsafeURLError, assert_public_http_url

    for u in (
        "http://10.0.0.1/",
        "http://192.168.1.1/admin",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[fc00::1]/",  # IPv6 ULA
        "http://[fe80::1]/",  # IPv6 link-local
        "http://[ff00::1]/",  # IPv6 multicast
    ):
        with pytest.raises(UnsafeURLError):
            assert_public_http_url(u)


def test_assert_public_http_url_rejects_localhost_aliases() -> None:
    from murano.security import UnsafeURLError, assert_public_http_url

    for u in (
        "http://localhost/",
        "http://localhost.localdomain/",
        "http://LOCALHOST:8080/admin",
    ):
        with pytest.raises(UnsafeURLError):
            assert_public_http_url(u)


def test_assert_public_http_url_rejects_ipv4_mapped_ipv6_loopback() -> None:
    """`::ffff:127.0.0.1` is IPv6 syntactically but encodes IPv4 loopback."""
    from murano.security import UnsafeURLError, assert_public_http_url

    with pytest.raises(UnsafeURLError):
        assert_public_http_url("http://[::ffff:127.0.0.1]/")


def test_assert_public_http_url_rejects_dns_to_private_address() -> None:
    """Hostname resolves to a private IP via DNS — must block.

    This is the DNS-rebinding-by-name vector: the attacker controls a
    real public domain that resolves to 10.x or 192.168.x.
    """
    from murano.security import UnsafeURLError, assert_public_http_url

    def fake_getaddrinfo(host, port):
        return [(2, 1, 6, "", ("10.0.0.42", 0))]

    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_http_url("http://evil.example/", resolver=fake_getaddrinfo)


def test_assert_public_http_url_accepts_public_address() -> None:
    """Don't block real public addresses."""
    from murano.security import assert_public_http_url

    # 93.184.216.34 is example.com (a real, publicly routed IP).
    def fake_getaddrinfo(host, port):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    # Should not raise.
    assert_public_http_url("http://example.com/", resolver=fake_getaddrinfo)


def test_assert_public_http_url_rejects_non_http_schemes() -> None:
    from murano.security import UnsafeURLError, assert_public_http_url

    for u in ("file:///etc/passwd", "ftp://example.com/", "javascript:alert(1)"):
        with pytest.raises(UnsafeURLError):
            assert_public_http_url(u)


def test_assert_public_http_url_dev_override_unlocks_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MURANO_ALLOW_PRIVATE_CAPTURES env var unlocks the gate for dev use."""
    from murano.security import assert_public_http_url

    monkeypatch.setenv("MURANO_ALLOW_PRIVATE_CAPTURES", "1")
    # Doesn't raise even though host is loopback.
    assert_public_http_url("http://127.0.0.1:8080/")


def test_assert_public_http_url_dev_override_ignored_when_allow_override_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production callers can disable the env override so an attacker-controlled
    env var doesn't unlock the gate for them."""
    from murano.security import UnsafeURLError, assert_public_http_url

    monkeypatch.setenv("MURANO_ALLOW_PRIVATE_CAPTURES", "1")
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("http://127.0.0.1/", allow_override=False)


def test_capture_url_blocks_private_targets(tmp_path) -> None:
    """End-to-end: capture_url refuses SSRF targets via the live extractor path."""
    import tempfile

    from murano.capture.web import CaptureError, capture_url
    from murano.config import Settings

    with tempfile.TemporaryDirectory() as td:
        td_p = pytest.importorskip("pathlib").Path(td).resolve()
        s = Settings(vault_root=td_p / "vault", data_root=td_p / "data")
        (td_p / "vault").mkdir()
        (td_p / "data").mkdir()
        for u in (
            "http://127.0.0.1:8000/leak",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/router",
            "http://localhost:3000/",
        ):
            with pytest.raises(CaptureError):
                capture_url(s, u)
