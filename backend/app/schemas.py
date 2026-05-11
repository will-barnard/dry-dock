"""Pydantic schemas for HTTP request/response bodies."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import (
    ApprovalGateKind,
    ApprovalGateStatus,
    TaskKind,
    TaskStatus,
    WorkerStatus,
)


class ProjectCreate(BaseModel):
    slug: str
    name: str
    github_owner: str
    github_repo: str
    default_branch: str = "main"
    system_prompt: str | None = None
    auto_approve_plans: bool = False
    auto_approve_merges: bool = False
    direct_push: bool = False


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    slug: str
    name: str
    github_owner: str
    github_repo: str
    default_branch: str
    auto_approve_plans: bool
    auto_approve_merges: bool
    direct_push: bool
    system_prompt: str | None
    created_at: datetime


class TaskCreate(BaseModel):
    kind: TaskKind
    title: str
    prompt: str
    # Optional — when omitted, the canonical pool for `kind` is used.
    required_pool: str = ""
    parent_task_id: uuid.UUID | None = None
    priority: int = 100
    min_ram_gb: int = 0
    min_context: int = 0
    preferred_model: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_id: uuid.UUID
    parent_task_id: uuid.UUID | None
    kind: TaskKind
    title: str
    status: TaskStatus
    priority: int
    required_pool: str
    branch_name: str | None
    attempt: int
    max_attempts: int
    preferred_model: str | None
    created_at: datetime
    updated_at: datetime


class TaskDetail(TaskOut):
    prompt: str
    payload: dict[str, Any]
    result: dict[str, Any] | None


class WorkerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    pool: str
    hostname: str
    hardware_class: str
    ram_gb: int
    installed_models: list[str]
    max_context: int
    gpu_vram_gb: int = 0
    gpu_model: str | None = None
    status: WorkerStatus
    last_heartbeat: datetime | None


class ApprovalDecision(BaseModel):
    approve: bool
    note: str | None = None


class ApprovalGateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    task_id: uuid.UUID
    kind: ApprovalGateKind
    status: ApprovalGateStatus
    payload: dict[str, Any]
    note: str | None
    created_at: datetime
