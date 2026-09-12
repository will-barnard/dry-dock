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

    # External generate-API auth. A dedicated key so third-party apps (e.g.
    # seedbook) can call POST /api/v1/generate without sharing the worker
    # fleet credential. Rotatable independently of WORKER_SHARED_SECRET. Empty
    # string disables the public generate API entirely (every call 503s) so a
    # deploy that never sets the key can't be probed.
    drydock_api_key: str = Field(default="", alias="DRYDOCK_API_KEY")

    # Hard ceiling for a single synchronous generate call before the API gives
    # up waiting on the worker. Local models on a busy Mac can be slow, so this
    # is generous; callers can pass a lower per-request timeout.
    generate_timeout_seconds: float = Field(
        default=120.0, alias="GENERATE_TIMEOUT_SECONDS"
    )

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

    # ── Pilot web search ───────────────────────────────────────────
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

    # ── Darkroom (image generation) ────────────────────────────────
    # Where rendered PNGs land. Backed by the `images_data` named volume in
    # docker-compose.yml — a fixed volume name is what survives a Beachhead
    # blue/green swap, same bet repos_data already makes.
    image_dir: str = Field(default="/var/lib/drydock/images", alias="IMAGE_DIR")
    # Name of the remote machine (as configured in REMOTE_MACHINES_JSON) that
    # hosts the imager. Empty disables auto-wake — jobs then just fail with
    # "no imager online" instead of trying to wake anything.
    image_machine: str = Field(default="", alias="IMAGE_MACHINE")
    # How long to wait for a woken machine to boot, start Docker, and get its
    # imager worker registered. Cold path is genuinely 60-150s on a sleeping
    # box, so this is generous by design.
    image_wake_timeout_seconds: float = Field(
        default=180.0, alias="IMAGE_WAKE_TIMEOUT_SECONDS"
    )
    # Ceiling on a single render once a worker has actually picked it up.
    image_job_timeout_seconds: float = Field(
        default=300.0, alias="IMAGE_JOB_TIMEOUT_SECONDS"
    )
    # Hard cap on batch size. Each image is a separate WS message, but a big
    # batch still monopolises the one GPU for minutes.
    image_max_batch: int = Field(default=4, alias="IMAGE_MAX_BATCH")
    # Delete rendered images older than this many days. 0 = keep forever.
    image_retention_days: int = Field(default=0, alias="IMAGE_RETENTION_DAYS")
    # Daily ceiling on images generated through the keyed API (0 = no cap).
    # Anyone holding DRYDOCK_API_KEY can spend the GPU; this is the same shape
    # of guard as WEB_SEARCH_DAILY_BUDGET.
    image_daily_budget: int = Field(default=0, alias="IMAGE_DAILY_BUDGET")
    # Default workflow template name the imager should use when none is given.
    image_default_workflow: str = Field(
        default="sdxl_txt2img", alias="IMAGE_DEFAULT_WORKFLOW"
    )
    # Workflow used when a request carries a source image. A txt2img template
    # has nowhere to put one, so we switch rather than silently ignore it.
    image_img2img_workflow: str = Field(
        default="sdxl_img2img", alias="IMAGE_IMG2IMG_WORKFLOW"
    )
    # Ceiling on an uploaded source image before resizing. Phone photos are
    # ~5MB; this is generous without letting someone post a 200MB TIFF.
    image_max_upload_mb: float = Field(default=25.0, alias="IMAGE_MAX_UPLOAD_MB")

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
