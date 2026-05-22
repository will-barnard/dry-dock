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

    # ── Operator web search (Phase 1) ──────────────────────────────
    # Global on/off switch. When False the orchestrator never calls a search
    # backend even if a conversation has the toggle on — the UI hides the
    # checkbox entirely.
    web_search_enabled: bool = Field(default=False, alias="WEB_SEARCH_ENABLED")
    # Currently only "searxng" is implemented; "tavily" is planned.
    web_search_backend: str = Field(default="searxng", alias="WEB_SEARCH_BACKEND")
    # Self-hosted SearXNG instance, expected to expose /search?format=json.
    searxng_url: str = Field(default="", alias="SEARXNG_URL")
    # How many results to feed the model per turn. More = richer context, but
    # also more tokens — each result's snippet is ~100-300 tokens, so 15 lands
    # around 3-5K tokens of search context. Bump higher (30+) if your worker
    # is configured with a 32K+ num_ctx and you want more comprehensive
    # recall; cap lower if you're seeing the model truncate.
    web_search_max_results: int = Field(default=15, alias="WEB_SEARCH_MAX_RESULTS")
    # Hard daily ceiling across all conversations. 0 disables the cap.
    web_search_daily_budget: int = Field(default=200, alias="WEB_SEARCH_DAILY_BUDGET")
    # Hard timeout for a single search call.
    web_search_timeout_seconds: float = Field(
        default=5.0, alias="WEB_SEARCH_TIMEOUT_SECONDS"
    )
    # Scout headless renderer (Phase C). Internal service that renders JS
    # pages. Empty url disables rendering (fetch falls back to static).
    renderer_url: str = Field(default="http://renderer:3000", alias="RENDERER_URL")
    # Must exceed the renderer's worst case (≈25s nav + 8s settle + grace).
    renderer_timeout_seconds: float = Field(
        default=45.0, alias="RENDERER_TIMEOUT_SECONDS"
    )

    # User-Agent for the fetch_url tool. Defaults to a mainstream browser
    # string — an honest bot UA gets 403'd by Cloudflare-fronted sites.
    web_fetch_user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        alias="WEB_FETCH_USER_AGENT",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
