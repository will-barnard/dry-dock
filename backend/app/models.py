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
    is_temp: Mapped[bool] = mapped_column(Boolean, default=False)
    temp_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    # Audit-trail row for an external action the orchestrator took on the
    # user's behalf (web search, etc). Not sent to the model in Phase 1 —
    # search results are injected into the prompt as a synthetic system
    # message at dispatch time. TOOL rows exist so the transcript UI can
    # show what was queried and what came back.
    TOOL = "tool"


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
    # Legacy Phase-1 flag, superseded by web_mode. Kept so old rows don't
    # break; the boot migration backfills web_mode from it.
    web_search_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Web access mode for this conversation:
    #   "off"    — plain chat, no web (default)
    #   "search" — Phase 1 pre-flight SearXNG injection; works on any model
    #   "tools"  — Phase 2 agentic loop (web_search + fetch_url); needs a
    #              tool-capable model
    web_mode: Mapped[str] = mapped_column(String(16), default="off")
    # Optional domain restriction. When set (e.g. "reverb.com"), every web
    # search this conversation runs is scoped with `site:<domain>`. Applies
    # to both search and tools modes.
    search_site: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    # TOOL-role rows store metadata about an external call the orchestrator
    # made: `tool_name` is e.g. "web_search", `tool_payload` is the raw
    # structured result (list of {title, url, snippet} for searches). Null
    # on USER/ASSISTANT/SYSTEM rows.
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)


class WebSearchUsage(Base):
    """One row per calendar day, counts global web searches that day. Single-
    user app so we don't track per-user; one row is plenty for the daily-cap
    safety net."""

    __tablename__ = "web_search_usage"

    day: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


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

    # `lazy="selectin"` makes bullets eager-load via a second SELECT IN query
    # whenever a CVEntry is loaded. The async session can't do implicit
    # lazy-load (raises MissingGreenlet on attribute access), and bullets are
    # tiny + always wanted alongside their entry, so this is the right default.
    bullets: Mapped[list["CVBullet"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan",
        order_by="CVBullet.sort_order", lazy="selectin",
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
    IMPORT = "import"             # parse a resume → incrementally merge into the library
    TAILOR = "tailor"             # (W2) select library items for a job description
    IMPROVE = "improve"           # (W3) rewrite a single bullet
    COVER_LETTER = "cover_letter" # (W4) draft a cover letter for an application
    TAG_BULLETS = "tag_bullets"   # auto-assign tags to bullets across the library


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


class ResumeApplication(Base):
    """A job you're applying to — the job description plus the tailored resume
    versions generated against it."""

    __tablename__ = "resume_applications"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    company: Mapped[str] = mapped_column(String(255), default="")
    role_title: Mapped[str] = mapped_column(String(255), default="")
    job_description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # `lazy="selectin"` — async sessions can't do implicit lazy-load, and the
    # workbench home iterates application.versions in the template to show the
    # version count. Cheap eager-load via SELECT IN.
    versions: Mapped[list["TailoredResume"]] = relationship(
        back_populates="application", cascade="all, delete-orphan",
        order_by="TailoredResume.version", lazy="selectin",
    )


class TailoredResume(Base):
    """One generated, tailored resume for an application. Each generate run
    produces a new version. `selection` is the model's structured pick
    (validated against the library); `rendered` is the Markdown the
    orchestrator assembled from it — deterministic, so formatting is
    consistent across versions."""

    __tablename__ = "tailored_resumes"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resume_applications.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    selection: Mapped[dict] = mapped_column(JSON, default=dict)  # the model's raw pick
    rendered: Mapped[str] = mapped_column(Text, default="")       # assembled Markdown
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    application: Mapped[ResumeApplication] = relationship(back_populates="versions")


class CoverLetter(Base):
    """A drafted cover letter for an application. Like TailoredResume, each
    generation is a new version — the body is Markdown rendered to PDF by the
    same WeasyPrint pipeline."""

    __tablename__ = "cover_letters"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resume_applications.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    body: Mapped[str] = mapped_column(Text, default="")  # the letter, in Markdown / plain prose
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── Scout module: site-knowledge / extraction recipes ──────────────


class SiteProfileStatus(str, enum.Enum):
    LEARNING = "learning"   # a learning job is figuring out the recipe
    ACTIVE = "active"       # has a validated, working recipe
    STALE = "stale"         # recipe started failing; needs re-learn
    FAILED = "failed"       # couldn't produce a working recipe


class ExtractionStrategy(str, enum.Enum):
    JSONLD = "jsonld"               # parse a JSON-LD block
    EMBEDDED_JSON = "embedded_json" # read a dotted path out of an embedded JSON blob
    SELECTORS = "selectors"         # CSS selectors per field
    API = "api"                     # page is backed by a JSON endpoint (Phase C)


class SiteProfile(Base):
    """Everything Scout knows about one domain. The heart is its active
    ExtractionRecipe — a validated description of where the useful data lives
    on that site, so fetch_url can pull structured fields instead of a wall
    of text."""

    __tablename__ = "site_profiles"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[SiteProfileStatus] = mapped_column(
        Enum(SiteProfileStatus, name="site_profile_status"),
        default=SiteProfileStatus.ACTIVE,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    recipes: Mapped[list["ExtractionRecipe"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan",
        order_by="ExtractionRecipe.version", lazy="selectin",
    )


class ExtractionRecipe(Base):
    """A versioned, validated recipe for extracting structured fields from one
    site. One recipe per profile is `active` at a time; re-learning adds a new
    version and only flips active once it validates."""

    __tablename__ = "extraction_recipes"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    site_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("site_profiles.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    strategy: Mapped[ExtractionStrategy] = mapped_column(
        Enum(ExtractionStrategy, name="extraction_strategy"),
        default=ExtractionStrategy.JSONLD,
    )
    # field name → location, interpreted per strategy:
    #   jsonld / embedded_json: a dotted path ("offers.price")
    #   selectors:             a CSS selector ("span.price")
    field_map: Mapped[dict] = mapped_column(JSON, default=dict)
    # How to turn a query into a results URL/API call for this site (stored
    # for a future "search the site directly" path; not wired in Phase A).
    search_strategy: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    needs_js: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(default=0.0)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    profile: Mapped[SiteProfile] = relationship(back_populates="recipes")
