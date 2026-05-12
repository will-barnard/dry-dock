"""Runtime configuration loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://drydock:drydock@postgres:5432/drydock",
        alias="DATABASE_URL",
    )
    database_url_sync: str = Field(
        default="postgresql://drydock:drydock@postgres:5432/drydock",
        alias="DATABASE_URL_SYNC",
    )

    # Worker auth — shared secret presented on WS connect.
    worker_shared_secret: str = Field(default="dev-secret-change-me", alias="WORKER_SHARED_SECRET")

    # GitHub integration
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_username: str = Field(default="", alias="GITHUB_USERNAME")

    # Public base URL (used for PR descriptions, etc.)
    drydock_base_url: str = Field(default="http://localhost", alias="DRYDOCK_BASE_URL")

    # Model defaults — workers can override based on what's installed.
    default_code_model: str = Field(default="qwen2.5-coder:32b", alias="DEFAULT_CODE_MODEL")
    default_planner_model: str = Field(default="qwen2.5-coder:32b", alias="DEFAULT_PLANNER_MODEL")

    # Filesystem
    repo_cache_dir: str = Field(default="/var/lib/drydock/repos", alias="REPO_CACHE_DIR")

    # Session cookie signing. Set this in Beachhead env to anything long and
    # random — if it ever changes, every existing browser session is invalidated.
    session_secret: str = Field(
        default="CHANGE-ME-dev-only-not-for-production", alias="SESSION_SECRET"
    )
    # Mark the session cookie Secure so browsers only send it over HTTPS.
    # True for Beachhead production deploys; flip to False if you ever serve
    # the orchestrator over plain HTTP for local dev.
    session_https_only: bool = Field(default=True, alias="SESSION_HTTPS_ONLY")

    # Remote machine wake/shutdown — see host-agent/README.md
    host_agent_url: str = Field(
        default="http://host.docker.internal:8088", alias="DRYDOCK_HOST_AGENT_URL"
    )
    host_agent_token: str = Field(default="", alias="DRYDOCK_HOST_AGENT_TOKEN")
    remote_machines_json: str = Field(default="[]", alias="REMOTE_MACHINES_JSON")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
