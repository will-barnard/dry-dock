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
    # Additive capability advertising. Absent from an un-upgraded worker's
    # register message, which pydantic fills with the default — so old and new
    # workers interoperate and the fleet upgrades one machine at a time.
    #   "chat"  — Ollama-backed task/chat work (the implicit default)
    #   "image" — ComfyUI diffusion work (the Darkroom `imager` pool)
    # Imagers also put {"workflows": [...], "checkpoints": [...]} in metadata.
    capabilities: list[str] = Field(default_factory=list)
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


# ──────────────────────────── Pilot chat messages ────────────────────────────
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
    # Full message history to feed the model: [{role, content}, ...]. dict
    # values can be nested (tool-role messages carry a tool_calls list).
    messages: list[dict[str, Any]]
    # OpenAI-style tool schema. None / empty → plain chat (no tool loop). When
    # present the worker runs a tool-calling loop and emits ChatToolCallMsg.
    tools: list[dict[str, Any]] | None = None


class ChatChunkMsg(BaseModel):
    """worker → orchestrator: one streamed delta of the assistant reply."""

    type: Literal["chat_chunk"] = "chat_chunk"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    delta: str


class ChatToolCallMsg(BaseModel):
    """worker → orchestrator: the model requested a tool call mid-turn. The
    orchestrator runs the tool and replies with a ChatToolResultMsg bearing
    the same tool_call_id."""

    type: Literal["chat_tool_call"] = "chat_tool_call"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    tool_call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatToolResultMsg(BaseModel):
    """orchestrator → worker: result of a worker-requested tool call."""

    type: Literal["chat_tool_result"] = "chat_tool_result"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    tool_call_id: str
    success: bool
    content: str = ""
    error: str | None = None


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


# ────────────────────────── Workbench job messages ──────────────────────────
#
# Workbench jobs (resume import, tailoring, bullet improvement) are one-shot,
# non-streaming inferences. The orchestrator sends a workbench_request; the
# worker runs a single `provider.chat` and returns a workbench_result. No
# chunks — the whole response comes back at once, which is fine because the
# orchestrator needs the complete (usually JSON) output to act on it anyway.


class WorkbenchRequestMsg(BaseModel):
    """orchestrator → worker: run one Workbench inference job."""

    type: Literal["workbench_request"] = "workbench_request"
    job_id: uuid.UUID
    kind: str  # import | tailor | improve
    model: str | None  # None → worker uses its DEFAULT_MODEL
    messages: list[dict[str, str]]


class WorkbenchResultMsg(BaseModel):
    """worker → orchestrator: a Workbench job finished (or failed)."""

    type: Literal["workbench_result"] = "workbench_result"
    job_id: uuid.UUID
    kind: str
    success: bool
    content: str = ""
    error: str | None = None


# ────────────────────────── Darkroom image messages ──────────────────────────
#
# Image generation is the one place that could NOT reuse the Workbench message
# pair. `run_workbench_job` is hard-wired to a single `provider.chat` returning
# a string; an un-upgraded worker handed an image job would answer with prose
# where the caller expects PNG bytes. So Darkroom gets its own pair, and the
# `capabilities` field on RegisterMsg (additive — pydantic defaults it for old
# workers) is how the orchestrator knows who can actually serve one.
#
# One ImageResultMsg per image, never one fat frame: a 1024² PNG is ~1-2 MB,
# so ~1.4-2.7 MB once base64'd, and a batch of four in a single frame would
# block the socket for everything else on that worker.


class ImageRequestMsg(BaseModel):
    """orchestrator → worker: render one image job on ComfyUI."""

    type: Literal["image_request"] = "image_request"
    job_id: uuid.UUID
    # Named workflow template the worker ships (e.g. "sdxl_txt2img"). The
    # worker owns the graph; the orchestrator owns the parameters.
    workflow: str = "sdxl_txt2img"
    # Escape hatch: a raw ComfyUI graph, which overrides `workflow` entirely.
    # Lets a new workflow be trialled from the orchestrator without rebuilding
    # and redeploying the worker on the Windows box.
    graph: dict[str, Any] | None = None
    prompt: str
    negative_prompt: str | None = None
    checkpoint: str | None = None  # None → worker's COMFYUI_DEFAULT_CHECKPOINT
    width: int = 1024
    height: int = 1024
    steps: int = 30
    cfg: float = 6.0
    sampler: str | None = None
    scheduler: str | None = None
    seed: int | None = None  # None → worker randomizes and reports what it used
    batch: int = 1  # capped orchestrator-side; see IMAGE_MAX_BATCH
    # img2img: a starting image, base64 PNG. The orchestrator has already
    # resized it to something the model was trained near, so the worker just
    # uploads it to ComfyUI and points the graph at it. Output dimensions come
    # from this image, not from width/height.
    init_image_b64: str | None = None
    # How much of the starting image to destroy. 1.0 is pure text-to-image and
    # is the default precisely so a workflow with no init image is unaffected;
    # ~0.3 retouches, ~0.6 restyles, ~0.8 keeps only the composition.
    denoise: float = 1.0


class ImageResultMsg(BaseModel):
    """worker → orchestrator: one finished image (or a whole-job failure).

    `index`/`total` let the orchestrator assemble a batch. A failure arrives as
    a single message with success=False and index=0.
    """

    type: Literal["image_result"] = "image_result"
    job_id: uuid.UUID
    index: int = 0
    total: int = 1
    success: bool
    image_b64: str = ""  # PNG bytes, base64-encoded
    seed: int | None = None
    # What ACTUALLY ran, as opposed to what was asked for. The generate API's
    # `model` field echoes the request and is a known trap; this one is
    # truthful and is the field worth logging.
    checkpoint: str | None = None
    width: int = 0
    height: int = 0
    elapsed_ms: int = 0
    error: str | None = None


class ImageProgressMsg(BaseModel):
    """worker → orchestrator: optional progress ping while a job renders.

    Purely cosmetic — the job row is authoritative. Workers may skip it.
    """

    type: Literal["image_progress"] = "image_progress"
    job_id: uuid.UUID
    step: int = 0
    total_steps: int = 0
    note: str | None = None


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
    | WorkbenchResultMsg
    | ImageResultMsg
    | ImageProgressMsg
)
