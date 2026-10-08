"""
Application configuration loaded from environment variables, with optional
local `.env` support through pydantic-settings.

Credentials and connection strings have no hardcoded runtime defaults.
"""

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings read from the process environment or local `.env`.

    Required keys (app will not start without these):
        DATABASE_URL, SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, OPENAI_API_KEY,
        LLM_PROVIDER, LLM_MODEL, EMBEDDING_MODEL
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ──
    database_url: str  # e.g. postgresql+asyncpg://user:pass@localhost:5432/pm_agent_dev

    @property
    def database_url_sync(self) -> str:
        """Sync variant for Alembic — strips the +asyncpg driver prefix."""
        return self.database_url.replace("+asyncpg", "")

    # ── Slack ──
    slack_bot_token: str          # xoxb-...
    slack_signing_secret: str     # from Slack app settings
    slack_app_token: Optional[str] = None  # xapp-... (only needed for Socket Mode)

    # ── LLM ──
    llm_provider: str             # e.g. openai
    llm_model: str                # e.g. gpt-4o
    openai_api_key: str           # sk-...

    # ── Embeddings ──
    embedding_model: str          # e.g. text-embedding-3-small

    # ── GitHub (optional) ──
    github_token: Optional[str] = None

    # ── Permissions ──
    global_admin_slack_ids: str = ""  # Comma-separated Slack user IDs

    @property
    def admin_slack_ids(self) -> list[str]:
        """Parse comma-separated global admin IDs into a list."""
        if not self.global_admin_slack_ids:
            return []
        return [
            sid.strip()
            for sid in self.global_admin_slack_ids.split(",")
            if sid.strip()
        ]

    # ── App ──
    log_level: str = "INFO"


@lru_cache()
def get_settings() -> Settings:
    """Cached settings singleton. Reads from .env once."""
    return Settings()
