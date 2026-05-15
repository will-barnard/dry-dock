"""Worker ↔ orchestrator message protocol.

Wire format: JSON lines over WebSocket. Every message has a `type` field.
Workers connect outbound from behind NAT, register their capabilities, then claim
and execute jobs over the same long-lived socket.

This module is the single source of truth for message shapes — both the
orchestrator and the worker import from it.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


# ────────────────────────── worker → orchestrator ──────────────────────────


class RegisterMsg(BaseModel):
    type: Literal["register"] = "register"
    name: str
    pool: str  # planner|coder|reviewer|tester|refactorer|docs|researcher
    hostname: str
    hardware_class: str  # mac-mini | macbook | windows-rtx3080 | linux | ...
    ram_gb: int
    installed_models: list[str]
    max_context: int
    # Optional GPU advertising — workers that don't have a dedicated GPU leave
    # these at the defaults. Used by the router to filter tasks that declare a
    # min_vram_gb requirement.
    gpu_vram_gb: int = 0
    gpu_model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HeartbeatMsg(BaseModel):
    type: Literal["heartbeat"] = "heartbeat"
    free_ram_gb: float | None = None
    current_task_id: uuid.UUID | None = None


class ClaimRequestMsg(BaseModel):
    """Worker is idle and ready for a new job."""

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


# ────────────────────────── orchestrator → worker ──────────────────────────


class WelcomeMsg(BaseModel):
    type: Literal["welcome"] = "welcome"
    worker_id: uuid.UUID
    server_version: str


class ClaimGrantMsg(BaseModel):
    """Server is handing the worker a task. Payload contains everything the
    runner needs to execute — repo info, prompt, model preference, etc."""

    type: Literal["claim_grant"] = "claim_grant"
    task_id: uuid.UUID
    run_id: uuid.UUID
    kind: str
    title: str
    prompt: str
    required_pool: str
    branch_name: str | None
    preferred_model: str | None
    project: dict[str, Any]  # {slug, github_owner, github_repo, default_branch, system_prompt}
    payload: dict[str, Any]


class CancelMsg(BaseModel):
    type: Literal["cancel"] = "cancel"
    task_id: uuid.UUID
    run_id: uuid.UUID
    reason: str | None = None


class PingMsg(BaseModel):
    type: Literal["ping"] = "ping"


# ────────────────────────── Operator chat messages ──────────────────────────
#
# Chat is a separate lifecycle from tasks: no run row, no git, no retries.
# The orchestrator picks a live worker directly and sends a chat_request;
# the worker streams chat_chunk deltas and finishes with chat_done (or
# chat_error). These ride the same WebSocket as task messages.


class ChatRequestMsg(BaseModel):
    """orchestrator → worker: answer one conversation turn."""

    type: Literal["chat_request"] = "chat_request"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID  # the pre-created empty assistant row to fill
    model: str | None  # None → worker uses its DEFAULT_MODEL
    # Full message history to feed the model: [{role, content}, ...].
    messages: list[dict[str, str]]


class ChatChunkMsg(BaseModel):
    """worker → orchestrator: one streamed delta of the assistant reply."""

    type: Literal["chat_chunk"] = "chat_chunk"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    delta: str


class ChatDoneMsg(BaseModel):
    """worker → orchestrator: the assistant turn is complete."""

    type: Literal["chat_done"] = "chat_done"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    content: str  # full final text (authoritative — the orchestrator persists this)
    tokens_in: int = 0
    tokens_out: int = 0


class ChatErrorMsg(BaseModel):
    """worker → orchestrator: the assistant turn failed."""

    type: Literal["chat_error"] = "chat_error"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    error: str


# Discriminated union for parsing inbound messages.
WorkerInbound = (
    RegisterMsg
    | HeartbeatMsg
    | ClaimRequestMsg
    | JobStartedMsg
    | LogChunkMsg
    | ArtifactMsg
    | ResultMsg
    | ErrorMsg
    | ChatChunkMsg
    | ChatDoneMsg
    | ChatErrorMsg
)
