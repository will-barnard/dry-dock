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

    ollama_base_url: str = Field(default="http://host.docker.internal:11434", alias="OLLAMA_BASE_URL")
    default_model: str = Field(default="qwen2.5-coder:32b", alias="DEFAULT_MODEL")

    worktree_root: str = Field(default="/app/worktrees", alias="WORKTREE_ROOT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
