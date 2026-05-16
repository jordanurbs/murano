"""Phase 1 — config/init smoke tests.

These cover the parts that do NOT touch the network or the real OS keychain:
- Settings loading with env overrides
- ensure_dirs is idempotent
- Round-trip save_settings / load_settings
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from murano import config as cfg


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    monkeypatch.setenv("MURANO_VAULT", str(vault))
    monkeypatch.setenv("MURANO_DATA", str(data))
    return tmp_path


def test_load_settings_uses_env_overrides(tmp_env: Path) -> None:
    s = cfg.load_settings()
    assert s.vault_root == (tmp_env / "vault").resolve()
    assert s.data_root == (tmp_env / "data").resolve()
    assert s.chat_model == cfg.DEFAULT_CHAT_MODEL
    assert s.embed_model == cfg.DEFAULT_EMBED_MODEL
    assert s.venice_base_url == cfg.VENICE_BASE_URL
    assert s.web_port == 3000


def test_ensure_dirs_creates_and_is_idempotent(tmp_env: Path) -> None:
    s = cfg.load_settings()
    created = cfg.ensure_dirs(s)
    assert set(created.keys()) == {"vault", "data", "logs"}
    assert s.vault_root.is_dir()
    assert s.data_root.is_dir()
    assert s.logs_dir.is_dir()

    created_again = cfg.ensure_dirs(s)
    assert created_again == {}


def test_settings_round_trip(tmp_env: Path) -> None:
    s = cfg.load_settings()
    cfg.ensure_dirs(s)
    s.chat_model = "my-custom-chat"
    s.embed_model = "my-custom-embed"
    s.web_port = 4242
    cfg.save_settings(s)

    for var in ("MURANO_CHAT_MODEL", "MURANO_EMBED_MODEL"):
        os.environ.pop(var, None)

    s2 = cfg.load_settings()
    assert s2.chat_model == "my-custom-chat"
    assert s2.embed_model == "my-custom-embed"
    assert s2.web_port == 4242
    assert s2.vault_root == s.vault_root


def test_chat_model_env_override_wins_over_config_file(tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = cfg.load_settings()
    cfg.ensure_dirs(s)
    s.chat_model = "from-config-file"
    cfg.save_settings(s)

    monkeypatch.setenv("MURANO_CHAT_MODEL", "from-env")
    s2 = cfg.load_settings()
    assert s2.chat_model == "from-env"
