"""Configuration: environment settings (.env) + mailbox/identity config (yaml).

Two sources, on purpose:
- Secrets and infra knobs -> environment (.env), never committed.
- Identities and accounts -> config.yaml, the user-editable description of "me" and
  which mailboxes to read. App-passwords are referenced by env-var name, not inlined.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from email_reader.paths import PROJECT_ROOT, get_data_dir


def _default_config_path() -> Path:
    """config.yaml lives in the data dir; fall back to the repo's config/ in dev."""
    data_path = get_data_dir() / "config.yaml"
    if data_path.exists():
        return data_path
    repo_path = PROJECT_ROOT / "config" / "config.yaml"
    if repo_path.exists():
        return repo_path
    return data_path


class Settings(BaseSettings):
    """Environment-driven settings (secrets, infra)."""

    model_config = SettingsConfigDict(
        env_file=str(get_data_dir() / ".env"), extra="ignore"
    )

    # Empty -> resolved to a SQLite file in the data dir (see _default_db_url).
    database_url: str = ""

    anthropic_api_key: str = ""
    extraction_model: str = "claude-opus-4-8"

    # Fernet key for encrypting mailbox app-passwords at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = ""

    telegram_bot_token: str = ""
    telegram_allowed_chat_id: int | None = None

    web_host: str = "127.0.0.1"
    web_port: int = 8000

    @field_validator("telegram_allowed_chat_id", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        if v in ("", None):
            return None
        return v

    @model_validator(mode="after")
    def _resolve_database_url(self) -> Settings:
        if not self.database_url:
            db_file = get_data_dir() / "email_reader.db"
            self.database_url = f"sqlite+aiosqlite:///{db_file}"
        return self

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_allowed_chat_id)


class AccountConfig(BaseModel):
    name: str
    host: str
    port: int = 993
    login: str
    password_ref: str = Field(description="Name of the env var holding the app-password")
    folders: list[str]

    @property
    def password(self) -> str:
        pw = os.environ.get(self.password_ref, "")
        if not pw:
            raise RuntimeError(
                f"App-password env var {self.password_ref!r} for account {self.name!r} is empty"
            )
        return pw


class AppConfig(BaseModel):
    """User-facing config from config.yaml.

    All fields default to empty: in the desktop build accounts and identities are
    managed via the UI and stored in the DB, so config.yaml is optional and only
    consumed for first-run bootstrap import.
    """

    identities: list[str] = Field(default_factory=list)
    normalize_plus_tags: bool = False
    followup_days: int = 3
    poll_interval_seconds: int = 120
    # Only sync mail newer than this many days (server arrival date). ~3 months.
    sync_window_days: int = 90
    # Only run the LLM agent on threads whose latest message is within this window,
    # to bound first-run backfill cost. 0 = no limit.
    extract_backfill_days: int = 30
    accounts: list[AccountConfig] = Field(default_factory=list)

    @property
    def identity_set(self) -> set[str]:
        return {addr.strip().lower() for addr in self.identities}


def load_app_config(path: Path | None = None) -> AppConfig:
    cfg_path = path or _default_config_path()
    if not cfg_path.exists():
        # No config.yaml: run with empty defaults (UI/DB-managed setup).
        return AppConfig()
    with cfg_path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return AppConfig.model_validate(raw)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_app_config() -> AppConfig:
    return load_app_config()
