from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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
    telegram_webhook_secret: SecretStr = SecretStr("")
    outbound_proxy_url: Secret[AnyHttpUrl] | None = None
    max_telegram_body_bytes: int = Field(default=1_048_576, ge=1_024, le=10_485_760)

    admin_email: str = "admin@example.com"
    admin_password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    cookie_secure: bool = True

    claude_bin: str = "claude"
    claude_timeout_seconds: int = Field(default=180, ge=10, le=900)
    repository_root: Path = Path("runtime/repositories")
    snapshot_root: Path = Path("runtime/snapshots")

    worker_poll_seconds: float = Field(default=2, gt=0, le=60)
    worker_max_attempts: int = Field(default=5, ge=1, le=20)
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def require_async_psycopg(cls, value: str) -> str:
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError("database_url must use postgresql+psycopg")
        return value

    @property
    def telegram_webhook_url(self) -> str:
        return f"{str(self.public_url).rstrip('/')}/webhooks/telegram"


@lru_cache
def get_settings() -> Settings:
    return Settings()
