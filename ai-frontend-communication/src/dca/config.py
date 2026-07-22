from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, Secret, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="DCA_",
        extra="ignore",
        case_sensitive=False,
    )

    public_url: AnyHttpUrl = "http://localhost:8000"  # type: ignore[assignment]
    database_url: str = "postgresql+psycopg://dca:dca@localhost:5432/dca"
    redis_url: str = "redis://localhost:6379/0"

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_mode: Literal["webhook", "polling"] = "webhook"
    telegram_webhook_secret: SecretStr = SecretStr("")
    outbound_proxy_url: Secret[AnyHttpUrl] | None = None
    max_telegram_body_bytes: int = Field(default=1_048_576, ge=1_024, le=10_485_760)

    github_webhook_secret: SecretStr = SecretStr("")
    max_github_webhook_body_bytes: int = Field(default=1_048_576, ge=1_024, le=10_485_760)
    repository_reconcile_seconds: int = Field(default=300, ge=30, le=86_400)

    session_secret: SecretStr = SecretStr("")
    cookie_secure: bool = True

    claude_bin: str = "claude"
    claude_session_root: Path = Path("runtime/claude-sessions")
    repository_root: Path = Path("runtime/repositories")
    snapshot_root: Path = Path("runtime/snapshots")

    worker_poll_seconds: float = Field(default=0.25, gt=0, le=60)
    worker_max_attempts: int = Field(default=5, ge=1, le=20)
    knowledge_concurrency: int = Field(default=5, ge=1, le=5)
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def require_async_psycopg(cls, value: str) -> str:
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError("database_url must use postgresql+psycopg")
        return value

    @field_validator("github_webhook_secret")
    @classmethod
    def require_strong_github_webhook_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if raw and len(raw) < 32:
            raise ValueError("github_webhook_secret must contain at least 32 characters")
        return value

    @property
    def telegram_webhook_url(self) -> str:
        return f"{str(self.public_url).rstrip('/')}/webhooks/telegram"

    @property
    def github_webhook_url(self) -> str:
        return f"{str(self.public_url).rstrip('/')}/webhooks/github"


@lru_cache
def get_settings() -> Settings:
    return Settings()
