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
    # num_ctx handed to Ollama on every call, and the value advertised to the
    # orchestrator for routing. These were separate concerns until now: the
    # value was advertised but never passed to inference, so Ollama used its
    # 4096 default and silently truncated every prompt from the front.
    max_context: int = Field(default=32768, alias="MAX_CONTEXT")

    # Sampling for the task runners (coder, planner, reviewer, ...). Ollama's
    # default temperature is 0.8, tuned for chat; for structured edits it
    # manufactures variety we do not want.
    temperature: float = Field(default=0.15, alias="TEMPERATURE")
    top_p: float = Field(default=0.9, alias="TOP_P")
    repeat_penalty: float = Field(default=1.05, alias="REPEAT_PENALTY")
    # Generated tokens share the context window with the prompt, so this is
    # carved out of max_context before the prompt budget is computed.
    num_predict: int = Field(default=8192, alias="NUM_PREDICT")
    # Fraction of the remaining window an assembled prompt may occupy. The
    # slack absorbs the retry paths, which replay the conversation plus the
    # previous assistant turn.
    prompt_budget_ratio: float = Field(default=0.7, alias="PROMPT_BUDGET_RATIO")

    # Operator chat and Workbench are conversational, not structured-edit
    # work, so they get their own (higher) temperature.
    chat_temperature: float = Field(default=0.7, alias="CHAT_TEMPERATURE")
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
