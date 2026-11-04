"""Application configuration.

Values come from environment variables (optionally loaded from a ``.env`` file
at the project root). Secrets are never written to the JSON data files; they
live only in the environment.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ---
    app_name: str = "Telegram Quiz Manager"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_debug: bool = False
    # Empty means "use PROJECT_ROOT".
    app_base_dir: str = ""

    # --- Secrets (required) ---
    secret_key: str = ""
    admin_username: str = "admin"
    admin_password: str = ""

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_mode: str = "polling"
    telegram_webhook_url: str = ""
    telegram_webhook_secret: str = ""

    # --- Scheduling ---
    default_timezone: str = "Asia/Riyadh"

    # --- Session ---
    session_cookie_secure: bool = False
    session_max_age: int = 12 * 60 * 60

    # --- Derived paths (not read from env) ---
    base_dir: Path = Field(default=PROJECT_ROOT)

    @field_validator("telegram_mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"polling", "webhook"}:
            raise ValueError("TELEGRAM_MODE must be 'polling' or 'webhook'")
        return value

    def model_post_init(self, __context: object) -> None:  # noqa: D105
        base = Path(self.app_base_dir).expanduser() if self.app_base_dir else PROJECT_ROOT
        object.__setattr__(self, "base_dir", base.resolve())

    # --- Paths ---
    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def log_dir(self) -> Path:
        return self.base_dir / "logs"

    @property
    def templates_dir(self) -> Path:
        # Templates and static assets ship with the code, so they always live
        # under the project root regardless of where data is stored.
        return PROJECT_ROOT / "app" / "templates"

    @property
    def static_dir(self) -> Path:
        return PROJECT_ROOT / "app" / "static"

    @property
    def dry_run(self) -> bool:
        """No bot token => the Telegram layer logs instead of sending."""
        return not self.telegram_bot_token.strip()

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.backup_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()

