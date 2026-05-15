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
    messages: list[dict[str, str]]


class ChatChunkMsg(BaseModel):
    type: Literal["chat_chunk"] = "chat_chunk"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    delta: str


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
