"""Worker configuration."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    orchestrator_url: str = Field(alias="ORCHESTRATOR_URL")
    worker_shared_secret: str = Field(alias="WORKER_SHARED_SECRET")
    worker_name: str = Field(alias="WORKER_NAME")
    worker_pool: str = Field(alias="WORKER_POOL")
    hardware_class: str = Field(default="macbook", alias="HARDWARE_CLASS")
    ram_gb: int = Field(default=16, alias="RAM_GB")
    max_context: int = Field(default=8192, alias="MAX_CONTEXT")
    # Optional GPU advertising. Macs leave these at the defaults; Windows /
    # Linux hosts with a discrete GPU set GPU_VRAM_GB and GPU_MODEL so the
    # dispatcher can route VRAM-dependent tasks to the right machines.
    gpu_vram_gb: int = Field(default=0, alias="GPU_VRAM_GB")
    gpu_model: str | None = Field(default=None, alias="GPU_MODEL")

    ollama_base_url: str = Field(default="http://host.docker.internal:11434", alias="OLLAMA_BASE_URL")
    default_model: str = Field(default="qwen2.5-coder:32b", alias="DEFAULT_MODEL")

    # GitHub credential used by GitWorkspace.clone() for private repos.
    # Reads-only is fine — workers never push. If left empty, clones fall
    # back to unauthenticated and will only succeed for public repos.
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_username: str = Field(default="", alias="GITHUB_USERNAME")

    worktree_root: str = Field(default="/app/worktrees", alias="WORKTREE_ROOT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
