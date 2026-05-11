"""Lifecycle actions for tasks and projects: delete, re-run, re-queue children.

These all run server-side under a transaction. Cascading deletes lean on the
FK ondelete rules in `models.py` (Runs, Events, Artifacts, ApprovalGates all
cascade from Task; Tasks cascade from Project). The Task→Task parent FK uses
SET NULL, so descendants must be deleted explicitly — handled here via BFS.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApprovalGate,
    ApprovalGateStatus,
    Project,
    Task,
    TaskStatus,
)

log = structlog.get_logger()


# Task states that can be re-run / deleted safely. We don't touch RUNNING or
# CLAIMED tasks because a worker is mid-flight — cancel first, then delete.
DELETABLE_STATUSES = {
    TaskStatus.PENDING,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.QUEUED,
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.REJECTED,
}

RERUNNABLE_STATUSES = {
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.REJECTED,
}


# ── descendants helper ────────────────────────────────────────────


async def _descendant_ids(session: AsyncSession, root_id: uuid.UUID) -> list[uuid.UUID]:
    """Return every task whose ancestry includes root_id (depth-first BFS)."""
    descendants: list[uuid.UUID] = []
    frontier: list[uuid.UUID] = [root_id]
    while frontier:
        result = await session.execute(
            select(Task.id).where(Task.parent_task_id.in_(frontier))
        )
        children = [row[0] for row in result.all()]
        if not children:
            break
        descendants.extend(children)
        frontier = children
    return descendants


# ── task actions ──────────────────────────────────────────────────


async def delete_task(session: AsyncSession, task: Task, *, cascade: bool = True) -> int:
    """Delete a task (and optionally its descendants). Returns count deleted."""
    ids = [task.id]
    if cascade:
        ids.extend(await _descendant_ids(session, task.id))
    # Delete from leaves up so parent FKs stay consistent during the operation.
    # Postgres handles FK cascades for Run/Event/Artifact/ApprovalGate.
    await session.execute(delete(Task).where(Task.id.in_(ids)))
    log.info("lifecycle.task_deleted", root=str(task.id), count=len(ids))
    return len(ids)


async def rerun_task(session: AsyncSession, task: Task) -> Task:
    """Reset a failed/rejected/cancelled task back to QUEUED.

    Old Run / Event / Artifact rows are kept for audit. Any PENDING approval
    gates on the task are cleared so a fresh run can produce a new one.
    """
    if task.status not in RERUNNABLE_STATUSES:
        raise ValueError(
            f"task is {task.status.value}; can only re-run "
            f"{', '.join(s.value for s in RERUNNABLE_STATUSES)}"
        )
    task.status = TaskStatus.QUEUED
    task.result = None
    # Drop stale pending gates (don't touch decided ones — those are history).
    await session.execute(
        delete(ApprovalGate).where(
            ApprovalGate.task_id == task.id,
            ApprovalGate.status == ApprovalGateStatus.PENDING,
        )
    )
    log.info("lifecycle.task_rerun", task=str(task.id))
    return task


async def requeue_failed_children(session: AsyncSession, parent: Task) -> int:
    """For a plan task whose fan-out went sideways: reset every failed,
    rejected, or cancelled descendant back to QUEUED. Returns count touched."""
    descendant_ids = await _descendant_ids(session, parent.id)
    if not descendant_ids:
        return 0
    rows = (await session.execute(
        select(Task).where(
            Task.id.in_(descendant_ids),
            Task.status.in_(RERUNNABLE_STATUSES),
        )
    )).scalars().all()
    for t in rows:
        t.status = TaskStatus.QUEUED
        t.result = None
    if rows:
        await session.execute(
            delete(ApprovalGate).where(
                ApprovalGate.task_id.in_([t.id for t in rows]),
                ApprovalGate.status == ApprovalGateStatus.PENDING,
            )
        )
    log.info("lifecycle.children_requeued", parent=str(parent.id), count=len(rows))
    return len(rows)


async def promote_ready_children(
    session: AsyncSession,
    task: Task,
    *,
    parent_branch: str | None = None,
) -> int:
    """Advance PENDING tasks that were waiting on `task` to QUEUED.

    Call this whenever a task either SUCCEEDS or enters AWAITING_APPROVAL for a
    merge gate (the code is written; children can proceed on its branch).

    If `parent_branch` is supplied, it is written into each promoted child's
    `branch_name` so the next worker checks out the right base branch.
    """
    result = await session.execute(
        select(Task).where(
            Task.parent_task_id == task.id,
            Task.status == TaskStatus.PENDING,
        )
    )
    children = list(result.scalars().all())
    for child in children:
        child.status = TaskStatus.QUEUED
        if parent_branch:
            child.branch_name = parent_branch
    if children:
        log.info("lifecycle.promoted_children", parent=str(task.id), count=len(children),
                 parent_branch=parent_branch)
    return len(children)


# ── approval actions ──────────────────────────────────────────────


async def reopen_approval(session: AsyncSession, gate: ApprovalGate) -> ApprovalGate:
    """Reset a decided approval gate back to PENDING so it can be decided again.

    The original payload (e.g. the plan content) is preserved; only the status
    and decided_* fields are cleared. Use cases: you rejected a plan and
    changed your mind, or you approved a plan whose children all failed and
    want a fresh decision before re-fanning out.
    """
    if gate.status == ApprovalGateStatus.PENDING:
        return gate
    gate.status = ApprovalGateStatus.PENDING
    gate.decided_at = None
    gate.decided_by = None
    gate.note = None
    # Move the parent task back to awaiting_approval so the UI surfaces it.
    task = await session.get(Task, gate.task_id)
    if task is not None:
        task.status = TaskStatus.AWAITING_APPROVAL
        task.updated_at = datetime.now(timezone.utc)
    log.info("lifecycle.approval_reopened", gate=str(gate.id))
    return gate


# ── project actions ───────────────────────────────────────────────


async def delete_project(session: AsyncSession, project: Project) -> None:
    """Hard-delete a project. Cascades to all tasks (which cascade to
    runs/events/artifacts/approvals). The orchestrator's cached git clone in
    /var/lib/drydock/repos/<slug> is left in place — harmless, and if the
    slug is reused for a different repo the next ensure_clone will reset
    the remote and re-fetch."""
    await session.delete(project)
    log.info("lifecycle.project_deleted", project=str(project.id), slug=project.slug)
