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
    # Public HTTPS URL of the Student Mini App (used for the bot's WebApp button
    # and deep links). Empty => derive from the request's own origin.
    mini_app_url: str = ""
    # Reject Mini App initData older than this many seconds (0 disables the check).
    telegram_auth_max_age: int = 24 * 60 * 60
    # Per-request timeout for Bot API calls. The library default (5s) is too
    # tight on slow links: a quiz can be delivered while we still time out
    # reading the reply, which makes a successful publish look like a failure.
    telegram_timeout: float = 30.0

    # --- Scheduling ---
    default_timezone: str = "Asia/Riyadh"

    # --- Database ---
    # When set (e.g. Railway's ``${{Postgres.DATABASE_URL}}``), the app uses
    # PostgreSQL; when empty it falls back to the local SQLite file. This is the
    # only switch between the two storage backends.
    database_url: str = ""
    # Upper bound on pooled PostgreSQL connections per process.
    db_pool_max: int = 10
    # Optional PostgreSQL schema (tests use it to isolate each run). Empty uses
    # the server default ("public").
    db_schema: str = ""

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
    def db_path(self) -> Path:
        """The single SQLite database file holding every collection."""
        return self.data_dir / "tqm.db"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def uses_postgres(self) -> bool:
        return bool(self.database_url.strip())

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

