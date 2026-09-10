"""Worker configuration."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, data):
        """Treat an empty env var as absent so the field default applies.

        docker-compose substitutes `${FOO}` with an empty string when FOO
        isn't in the --env-file, and the compose template lists variables that
        not every worker sets — an imager has no MAX_CONTEXT, for instance.
        Without this, that empty string reaches a typed field and the container
        crash-loops on `unable to parse string as an integer` before logging is
        even configured, which is an opaque way to say "you left a variable
        out". Every field whose default is "" or None is unaffected.
        """
        if isinstance(data, dict):
            return {
                k: v for k, v in data.items()
                if not (isinstance(v, str) and v.strip() == "")
            }
        return data

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

    # ── Darkroom / ComfyUI (imager workers only) ──────────────────
    # An imager is an ordinary worker with WORKER_POOL=imager that talks to
    # ComfyUI on its host instead of Ollama. Everything else — registration,
    # heartbeat, reconnect, workers.sh — is unchanged.
    comfyui_base_url: str = Field(
        default="http://host.docker.internal:8188", alias="COMFYUI_BASE_URL"
    )
    # Used when a request doesn't pin one. Blank → the template's own default,
    # and failing that whatever ComfyUI reports first.
    comfyui_default_checkpoint: str = Field(default="", alias="COMFYUI_DEFAULT_CHECKPOINT")
    # Ceiling for one render, including a cold checkpoint load.
    comfyui_timeout_seconds: float = Field(default=300.0, alias="COMFYUI_TIMEOUT_SECONDS")
    # How long to wait for ComfyUI to answer before registering. This exists
    # because of boot ordering: the container has restart:unless-stopped, so on
    # a machine that reboots it comes back the moment Docker does — typically
    # well before ComfyUI has finished importing torch. Registering in that
    # window advertises an empty checkpoint list, and the Darkroom dropdown
    # stays empty until someone restarts the worker by hand.
    comfyui_startup_wait_seconds: float = Field(
        default=240.0, alias="COMFYUI_STARTUP_WAIT_SECONDS"
    )
    # Override the advertised capability list (comma-separated). Normally left
    # blank — it's derived from WORKER_POOL.
    worker_capabilities: str = Field(default="", alias="WORKER_CAPABILITIES")

    # GitHub credential used by GitWorkspace.clone() for private repos.
    # Reads-only is fine — workers never push. If left empty, clones fall
    # back to unauthenticated and will only succeed for public repos.
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_username: str = Field(default="", alias="GITHUB_USERNAME")

    worktree_root: str = Field(default="/app/worktrees", alias="WORKTREE_ROOT")

    # Escape hatch for the eval harness and offline testing: when set, every
    # GitWorkspace clones from this URL instead of building a GitHub one from
    # owner/repo. A file:// path pointed at a local bare repo makes runner
    # behaviour reproducible with no network and no GitHub state. Leave unset
    # in production — the workers should always clone the real project.
    clone_url_override: str = Field(default="", alias="CLONE_URL_OVERRIDE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


    @property
    def capabilities(self) -> list[str]:
        """What this worker advertises it can do.

        Derived from the pool so no existing env file has to change: every
        current worker is a chat worker, and only `imager` is an image worker.
        WORKER_CAPABILITIES overrides if you ever need a hybrid.
        """
        if self.worker_capabilities.strip():
            return [c.strip() for c in self.worker_capabilities.split(",") if c.strip()]
        return ["image"] if self.worker_pool == "imager" else ["chat"]

    @property
    def is_imager(self) -> bool:
        return "image" in self.capabilities


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
