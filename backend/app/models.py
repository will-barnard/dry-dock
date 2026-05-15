"""SQLAlchemy ORM models for dry-dock."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db import Base


class TaskKind(str, enum.Enum):
    PLAN = "plan"
    CODE = "code"
    REVIEW = "review"
    TEST = "test"
    REFACTOR = "refactor"
    DOCS = "docs"
    RESEARCH = "research"
    VALIDATE = "validate"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class ApprovalGateKind(str, enum.Enum):
    PLAN = "plan"
    MERGE = "merge"


class ApprovalGateStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class WorkerStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Setting(Base):
    """Key/value app-wide settings.

    Used for things that need to persist across deploys but aren't worth a
    dedicated table — e.g. role→model assignments. Keys are namespaced with a
    dotted prefix: 'role_model.coder', 'role_model.planner', etc.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    github_owner: Mapped[str] = mapped_column(String(255))
    github_repo: Mapped[str] = mapped_column(String(255))
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    auto_approve_plans: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_approve_merges: Mapped[bool] = mapped_column(Boolean, default=False)
    direct_push: Mapped[bool] = mapped_column(Boolean, default=False)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Shell commands run by the validator pool after each code/refactor task.
    # JSON list of strings; empty list means "no automated validation."
    validate_commands: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tasks: Mapped[list["Task"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[TaskKind] = mapped_column(Enum(TaskKind, name="task_kind"))
    title: Mapped[str] = mapped_column(String(500))
    prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"), default=TaskStatus.PENDING, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=100)
    required_pool: Mapped[str] = mapped_column(String(64))  # planner|coder|reviewer|...
    min_ram_gb: Mapped[int] = mapped_column(Integer, default=0)
    min_context: Mapped[int] = mapped_column(Integer, default=0)
    min_vram_gb: Mapped[int] = mapped_column(Integer, default=0)
    preferred_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    branch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="tasks")
    parent: Mapped["Task | None"] = relationship(remote_side="Task.id")
    runs: Mapped[list["Run"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    approvals: Mapped[list["ApprovalGate"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_tasks_project_status", "project_id", "status"),
        Index("ix_tasks_pool_status", "required_pool", "status"),
    )


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    pool: Mapped[str] = mapped_column(String(64), index=True)
    hostname: Mapped[str] = mapped_column(String(255))
    hardware_class: Mapped[str] = mapped_column(String(64))  # mac-mini | macbook | linux | ...
    ram_gb: Mapped[int] = mapped_column(Integer)
    installed_models: Mapped[list] = mapped_column(JSON, default=list)
    max_context: Mapped[int] = mapped_column(Integer, default=8192)
    gpu_vram_gb: Mapped[int] = mapped_column(Integer, default=0)
    gpu_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[WorkerStatus] = mapped_column(
        Enum(WorkerStatus, name="worker_status"), default=WorkerStatus.OFFLINE
    )
    current_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_blob: Mapped[dict] = mapped_column("metadata", JSON, default=dict)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workers.id", ondelete="SET NULL"), nullable=True
    )
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus, name="task_status"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tokens_in: Mapped[int] = mapped_column(BigInteger, default=0)
    tokens_out: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_blob: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    task: Mapped[Task] = relationship(back_populates="runs")
    events: Mapped[list["Event"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kind: Mapped[str] = mapped_column(String(32))  # log | tool_call | tool_result | error | status
    stream: Mapped[str] = mapped_column(String(16), default="stdout")  # stdout|stderr|system
    body: Mapped[str] = mapped_column(Text)

    run: Mapped[Run] = relationship(back_populates="events")


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32))  # patch | file | text | summary
    name: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    metadata_blob: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Conversation(Base):
    """An Operator-module chat thread. Independent of projects/tasks — chat is
    its own lifecycle that only borrows the worker fleet for inference."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(255), default="New conversation")
    # Which worker pool answers this thread's turns, and an optional model
    # override. Defaults are applied at creation time by the route.
    pool: Mapped[str] = mapped_column(String(64), default="researcher")
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["ConversationMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole, name="message_role"))
    content: Mapped[str] = mapped_column(Text, default="")
    # Assistant messages start `complete=False` (the worker is streaming); they
    # flip to True on chat_done. `error` is set if the turn failed.
    complete: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)


class ApprovalGate(Base):
    __tablename__ = "approval_gates"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    kind: Mapped[ApprovalGateKind] = mapped_column(Enum(ApprovalGateKind, name="approval_gate_kind"))
    status: Mapped[ApprovalGateStatus] = mapped_column(
        Enum(ApprovalGateStatus, name="approval_gate_status"),
        default=ApprovalGateStatus.PENDING,
    )
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[Task] = relationship(back_populates="approvals")


# ── Workbench module: the CV library ───────────────────────────────
#
# A structured, granular store of CV content. Entries (jobs / projects /
# education) are containers; bullets are the reusable, independently
# selectable line items within them; skills are independent items grouped
# by category. The tailoring workflow (W2) reads the whole library and picks
# the best-fit items for a given job description.


class CVEntryKind(str, enum.Enum):
    EXPERIENCE = "experience"
    PROJECT = "project"
    EDUCATION = "education"


class CVProfile(Base):
    """Header / contact block. Single-row table in practice, but keyed by id
    so we don't have to special-case 'the one row'."""

    __tablename__ = "cv_profile"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    headline: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # {linkedin, github, website, ...} — free-form so new link types don't
    # need a migration.
    links: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CVSummary(Base):
    """A summary-paragraph variant. You might keep a few — 'default',
    'leadership-focused', 'data-focused' — and the tailoring step picks one
    (or generates a fresh one seeded from these)."""

    __tablename__ = "cv_summaries"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    label: Mapped[str] = mapped_column(String(128), default="default")
    text: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CVEntry(Base):
    """A job, project, or education entry — a container for bullets."""

    __tablename__ = "cv_entries"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    kind: Mapped[CVEntryKind] = mapped_column(Enum(CVEntryKind, name="cv_entry_kind"), index=True)
    organization: Mapped[str] = mapped_column(String(255))  # company / project / school
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)  # job title / degree
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)  # for projects
    # Dates are display strings, not date types — resumes use "Oct '25",
    # "Present", bare years. Flexible strings are the right call here.
    start_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    end_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The italic line under education entries ("Graduated with Distinction…").
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    bullets: Mapped[list["CVBullet"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan",
        order_by="CVBullet.sort_order",
    )


class CVBullet(Base):
    """A reusable line item under an entry — the unit the tailoring step
    selects from."""

    __tablename__ = "cv_bullets"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cv_entries.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    # Skills / themes for matching against a job description — "python",
    # "ci-cd", "leadership". Gives the model handles, not just prose.
    tags: Mapped[list] = mapped_column(JSON, default=list)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    entry: Mapped[CVEntry] = relationship(back_populates="bullets")


class CVSkill(Base):
    """One skill, grouped by category for display."""

    __tablename__ = "cv_skills"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    category: Mapped[str] = mapped_column(String(128))  # "Web Development", ...
    name: Mapped[str] = mapped_column(String(128))  # "Spring Boot", ...
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkbenchJobKind(str, enum.Enum):
    IMPORT = "import"     # parse a resume → incrementally merge into the library
    TAILOR = "tailor"     # (W2) select library items for a job description
    IMPROVE = "improve"   # (W3) rewrite a single bullet


class WorkbenchJobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class WorkbenchJob(Base):
    """A unit of agent work for the Workbench module — kept separate from the
    Engineer-module Task table because it has no git/DAG/approval lifecycle.
    A job is dispatched to a worker, runs one non-streaming inference, and the
    orchestrator applies the structured result (e.g. merging imported CV items).
    """

    __tablename__ = "workbench_jobs"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    kind: Mapped[WorkbenchJobKind] = mapped_column(
        Enum(WorkbenchJobKind, name="workbench_job_kind")
    )
    status: Mapped[WorkbenchJobStatus] = mapped_column(
        Enum(WorkbenchJobStatus, name="workbench_job_status"),
        default=WorkbenchJobStatus.PENDING,
    )
    # Job inputs (for import: {"resume_text": "..."}) and the structured
    # outcome (for import: {"counts": {...}, "applied": {...}, "raw": {...}}).
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
