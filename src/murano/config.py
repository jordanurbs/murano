"""Configuration: paths, settings, and Venice API key storage in the OS keychain.

Layout (per MURANO_PLAN.md §6, §7):

    ~/murano/vault/        canonical source of truth, Obsidian-compatible
    ~/.murano/             derived index (rebuildable from vault)
    ├── chunks.db
    ├── summary_tree.db
    ├── config.toml
    └── logs/

Environment overrides:
    MURANO_VAULT           override vault root
    MURANO_DATA            override data root
    MURANO_CHAT_MODEL      override default chat model
    MURANO_EMBED_MODEL     override default embedding model
"""

from __future__ import annotations

import contextlib
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import keyring
import tomli_w

KEYRING_SERVICE = "murano"
KEYRING_USERNAME = "venice-api-key"

VENICE_BASE_URL = "https://api.venice.ai/api/v1"
DEFAULT_CHAT_MODEL = "qwen-3-6-plus"
# Venice's actual catalog ID for the Qwen3 Embedding 8B model. The plan
# documents this as `qwen3-embedding-8b` (the HuggingFace name); Venice
# exposes it under the `text-embedding-*` namespace.
DEFAULT_EMBED_MODEL = "text-embedding-qwen3-8b"


def default_vault_root() -> Path:
    env = os.environ.get("MURANO_VAULT")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / "murano" / "vault"


def default_data_root() -> Path:
    env = os.environ.get("MURANO_DATA")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".murano"


@dataclass
class Settings:
    """All overridable user settings. Persisted as TOML in ~/.murano/config.toml."""

    vault_root: Path = field(default_factory=default_vault_root)
    data_root: Path = field(default_factory=default_data_root)
    chat_model: str = DEFAULT_CHAT_MODEL
    embed_model: str = DEFAULT_EMBED_MODEL
    venice_base_url: str = VENICE_BASE_URL
    web_port: int = 3000

    @property
    def config_path(self) -> Path:
        return self.data_root / "config.toml"

    @property
    def chunks_db(self) -> Path:
        return self.data_root / "chunks.db"

    @property
    def summary_tree_db(self) -> Path:
        return self.data_root / "summary_tree.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    def to_toml_dict(self) -> dict:
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, Path):
                d[k] = str(v)
        return d


def load_settings() -> Settings:
    """Load settings from ~/.murano/config.toml, applying env overrides on top."""
    s = Settings()
    if s.config_path.exists():
        with open(s.config_path, "rb") as f:
            data = tomllib.load(f)
        for key, value in data.items():
            if not hasattr(s, key):
                continue
            if key in {"vault_root", "data_root"}:
                value = Path(value).expanduser()
            setattr(s, key, value)

    if env := os.environ.get("MURANO_CHAT_MODEL"):
        s.chat_model = env
    if env := os.environ.get("MURANO_EMBED_MODEL"):
        s.embed_model = env
    if env := os.environ.get("MURANO_VAULT"):
        s.vault_root = Path(env).expanduser().resolve()
    if env := os.environ.get("MURANO_DATA"):
        s.data_root = Path(env).expanduser().resolve()

    return s


def save_settings(settings: Settings) -> None:
    """Persist settings to ~/.murano/config.toml."""
    settings.data_root.mkdir(parents=True, exist_ok=True)
    with open(settings.config_path, "wb") as f:
        tomli_w.dump(settings.to_toml_dict(), f)


def ensure_dirs(settings: Settings) -> dict[str, Path]:
    """Create the canonical vault + derived data directories. Idempotent."""
    created: dict[str, Path] = {}
    for label, path in [
        ("vault", settings.vault_root),
        ("data", settings.data_root),
        ("logs", settings.logs_dir),
    ]:
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True)
        if not existed:
            created[label] = path
    return created


def get_api_key() -> str | None:
    """Read the Venice API key from the OS keychain."""
    return keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)


def set_api_key(key: str) -> None:
    """Store the Venice API key in the OS keychain."""
    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, key)


def delete_api_key() -> None:
    """Remove the Venice API key from the OS keychain (if present)."""
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
