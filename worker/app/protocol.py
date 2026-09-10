"""Worker-side mirror of the wire protocol.

Kept deliberately minimal: this isn't shared as a package because we want the
worker repo to be self-contained for `docker compose up` on a Mac. The
orchestrator-side definitions in `backend/app/orchestrator/protocol.py` are
authoritative — keep these aligned.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class RegisterMsg(BaseModel):
    type: Literal["register"] = "register"
    name: str
    pool: str
    hostname: str
    hardware_class: str
    ram_gb: int
    installed_models: list[str]
    max_context: int
    # Additive capability advertising — an un-upgraded worker simply omits it.
    #   "chat"  — Ollama-backed task/chat work (the implicit default)
    #   "image" — ComfyUI diffusion work (the Darkroom `imager` pool)
    # Imagers also put {"workflows": [...], "checkpoints": [...]} in metadata.
    capabilities: list[str] = Field(default_factory=list)
    gpu_vram_gb: int = 0
    gpu_model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HeartbeatMsg(BaseModel):
    type: Literal["heartbeat"] = "heartbeat"
    free_ram_gb: float | None = None
    current_task_id: uuid.UUID | None = None


class ClaimRequestMsg(BaseModel):
    type: Literal["claim_request"] = "claim_request"


class JobStartedMsg(BaseModel):
    type: Literal["job_started"] = "job_started"
    task_id: uuid.UUID
    run_id: uuid.UUID


class LogChunkMsg(BaseModel):
    type: Literal["log"] = "log"
    task_id: uuid.UUID
    run_id: uuid.UUID
    stream: Literal["stdout", "stderr", "system"] = "stdout"
    body: str


class ArtifactMsg(BaseModel):
    type: Literal["artifact"] = "artifact"
    task_id: uuid.UUID
    run_id: uuid.UUID
    kind: Literal["patch", "file", "text", "summary", "review", "test_report"]
    name: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResultMsg(BaseModel):
    type: Literal["result"] = "result"
    task_id: uuid.UUID
    run_id: uuid.UUID
    success: bool
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    model_used: str | None = None


class ErrorMsg(BaseModel):
    type: Literal["error"] = "error"
    task_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    code: str
    message: str
    retryable: bool = True


class ClaimGrantMsg(BaseModel):
    type: Literal["claim_grant"]
    task_id: uuid.UUID
    run_id: uuid.UUID
    kind: str
    title: str
    prompt: str
    required_pool: str
    branch_name: str | None
    preferred_model: str | None
    project: dict[str, Any]
    payload: dict[str, Any]


class WelcomeMsg(BaseModel):
    type: Literal["welcome"]
    worker_id: uuid.UUID
    server_version: str


# ── Operator chat messages (mirror of backend/app/orchestrator/protocol.py) ──


class ChatRequestMsg(BaseModel):
    type: Literal["chat_request"]
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    model: str | None
    # dict[str, Any] (not str) — tool-role messages carry nested tool_calls.
    messages: list[dict[str, Any]]
    # OpenAI-style tool schema. None / empty → plain chat (no tool loop).
    tools: list[dict[str, Any]] | None = None


class ChatChunkMsg(BaseModel):
    type: Literal["chat_chunk"] = "chat_chunk"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    delta: str


class ChatToolCallMsg(BaseModel):
    """worker → orchestrator: the model wants to call a tool. The orchestrator
    runs it and replies with a ChatToolResultMsg carrying the same id."""
    type: Literal["chat_tool_call"] = "chat_tool_call"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    tool_call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatToolResultMsg(BaseModel):
    """orchestrator → worker: result of a tool call the worker requested."""
    type: Literal["chat_tool_result"]
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    tool_call_id: str
    success: bool
    content: str = ""
    error: str | None = None


class ChatDoneMsg(BaseModel):
    type: Literal["chat_done"] = "chat_done"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    content: str
    tokens_in: int = 0
    tokens_out: int = 0


class ChatErrorMsg(BaseModel):
    type: Literal["chat_error"] = "chat_error"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    error: str


# ── Workbench job messages (mirror of backend protocol) ──


class WorkbenchRequestMsg(BaseModel):
    type: Literal["workbench_request"]
    job_id: uuid.UUID
    kind: str
    model: str | None
    messages: list[dict[str, str]]


class WorkbenchResultMsg(BaseModel):
    type: Literal["workbench_result"] = "workbench_result"
    job_id: uuid.UUID
    kind: str
    success: bool
    content: str = ""
    error: str | None = None


# ── Darkroom image messages (mirror of backend protocol) ──
#
# Image work could not reuse the Workbench pair: run_workbench_job is
# hard-wired to one provider.chat returning a string. One result message per
# image, never one fat frame — a 1024² PNG is ~1.4-2.7 MB once base64'd.


class ImageRequestMsg(BaseModel):
    type: Literal["image_request"]
    job_id: uuid.UUID
    workflow: str = "sdxl_txt2img"
    # Raw ComfyUI graph; overrides `workflow` when present.
    graph: dict[str, Any] | None = None
    prompt: str
    negative_prompt: str | None = None
    checkpoint: str | None = None
    width: int = 1024
    height: int = 1024
    steps: int = 30
    cfg: float = 6.0
    sampler: str | None = None
    scheduler: str | None = None
    seed: int | None = None
    batch: int = 1
    # img2img: base64 PNG to start from, already sized by the orchestrator.
    init_image_b64: str | None = None
    # 1.0 = ignore the init image entirely (plain txt2img), so this default
    # keeps every existing workflow behaving exactly as before.
    denoise: float = 1.0


class ImageResultMsg(BaseModel):
    type: Literal["image_result"] = "image_result"
    job_id: uuid.UUID
    index: int = 0
    total: int = 1
    success: bool
    image_b64: str = ""
    seed: int | None = None
    # What actually ran, not what was asked for.
    checkpoint: str | None = None
    width: int = 0
    height: int = 0
    elapsed_ms: int = 0
    error: str | None = None


class ImageProgressMsg(BaseModel):
    type: Literal["image_progress"] = "image_progress"
    job_id: uuid.UUID
    step: int = 0
    total_steps: int = 0
    note: str | None = None
