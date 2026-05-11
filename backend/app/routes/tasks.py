"""Task endpoints: create, list, approve/reject, fetch detail."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import (
    ApprovalGate,
    ApprovalGateKind,
    ApprovalGateStatus,
    Project,
    Task,
    TaskKind,
    TaskStatus,
)
from app.orchestrator.dispatcher import dispatcher
from app.orchestrator.event_bus import bus
from app.orchestrator.lifecycle import (
    DELETABLE_STATUSES,
    delete_task as do_delete_task,
    requeue_failed_children as do_requeue_failed_children,
    rerun_task as do_rerun_task,
)
from app.orchestrator.planner import materialize_plan
from app.orchestrator.pools import pool_for_kind
from app.schemas import (
    ApprovalDecision,
    ApprovalGateOut,
    TaskCreate,
    TaskDetail,
    TaskOut,
)

router = APIRouter(prefix="/api/projects/{project_id}/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    project_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> list[Task]:
    result = await session.execute(
        select(Task)
        .where(Task.project_id == project_id)
        .order_by(Task.created_at.desc())
    )
    return list(result.scalars().all())


@router.post("", response_model=TaskOut, status_code=201)
async def create_task(
    project_id: uuid.UUID,
    body: TaskCreate,
    session: AsyncSession = Depends(get_session),
) -> Task:
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")

    # Empty `required_pool` means "use the canonical pool for this kind".
    required_pool = (body.required_pool or "").strip() or pool_for_kind(body.kind)

    # Top-level user-created tasks queue immediately if the project allows it
    # for that kind; planner tasks always go through approval first when gated.
    starts_queued = True
    if body.kind == TaskKind.PLAN and not project.auto_approve_plans:
        starts_queued = True  # plan runs, but its output requires approval before fan-out
    task = Task(
        project_id=project_id,
        kind=body.kind,
        title=body.title,
        prompt=body.prompt,
        required_pool=required_pool,
        parent_task_id=body.parent_task_id,
        priority=body.priority,
        min_ram_gb=body.min_ram_gb,
        min_context=body.min_context,
        preferred_model=body.preferred_model,
        payload=body.payload,
        status=TaskStatus.QUEUED if starts_queued else TaskStatus.PENDING,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    dispatcher.poke()
    return task


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task(
    project_id: uuid.UUID, task_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Task:
    task = await session.get(Task, task_id)
    if not task or task.project_id != project_id:
        raise HTTPException(404, "task not found")
    return task


@router.delete("/{task_id}", status_code=204, response_class=Response)
async def delete_task_api(
    project_id: uuid.UUID, task_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Response:
    task = await session.get(Task, task_id)
    if not task or task.project_id != project_id:
        raise HTTPException(404, "task not found")
    if task.status not in DELETABLE_STATUSES:
        raise HTTPException(409, f"task is {task.status.value} — cancel it first")
    await do_delete_task(session, task, cascade=True)
    await session.commit()
    return Response(status_code=204)


@router.post("/{task_id}/rerun", response_model=TaskOut)
async def rerun_task_api(
    project_id: uuid.UUID, task_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Task:
    task = await session.get(Task, task_id)
    if not task or task.project_id != project_id:
        raise HTTPException(404, "task not found")
    try:
        await do_rerun_task(session, task)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    await session.commit()
    await session.refresh(task)
    dispatcher.poke()
    return task


@router.post("/{task_id}/rerun-failed-children", response_model=dict)
async def requeue_children_api(
    project_id: uuid.UUID, task_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    task = await session.get(Task, task_id)
    if not task or task.project_id != project_id:
        raise HTTPException(404, "task not found")
    n = await do_requeue_failed_children(session, task)
    await session.commit()
    dispatcher.poke()
    return {"requeued": n}


@router.get("/{task_id}/approvals", response_model=list[ApprovalGateOut])
async def list_approvals(
    project_id: uuid.UUID, task_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> list[ApprovalGate]:
    result = await session.execute(
        select(ApprovalGate)
        .where(ApprovalGate.task_id == task_id)
        .order_by(ApprovalGate.created_at.desc())
    )
    return list(result.scalars().all())


@router.post("/{task_id}/approvals/{gate_id}", response_model=ApprovalGateOut)
async def decide_approval(
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    gate_id: uuid.UUID,
    body: ApprovalDecision,
    session: AsyncSession = Depends(get_session),
) -> ApprovalGate:
    gate = await session.get(ApprovalGate, gate_id)
    if not gate or gate.task_id != task_id:
        raise HTTPException(404, "approval gate not found")
    if gate.status != ApprovalGateStatus.PENDING:
        raise HTTPException(409, "gate already decided")

    gate.status = ApprovalGateStatus.APPROVED if body.approve else ApprovalGateStatus.REJECTED
    gate.note = body.note
    gate.decided_at = datetime.now(timezone.utc)
    gate.decided_by = "user"

    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "task not found")

    if gate.kind == ApprovalGateKind.PLAN and body.approve:
        # Materialize the plan into child tasks.
        await materialize_plan(session, task)
        task.status = TaskStatus.SUCCEEDED
    elif gate.kind == ApprovalGateKind.PLAN and not body.approve:
        task.status = TaskStatus.REJECTED
    elif gate.kind == ApprovalGateKind.MERGE and body.approve:
        # The actual merge is performed asynchronously by the merge service.
        task.status = TaskStatus.SUCCEEDED
    elif gate.kind == ApprovalGateKind.MERGE and not body.approve:
        task.status = TaskStatus.REJECTED

    await session.commit()
    await session.refresh(gate)
    await bus.publish(
        bus.task_topic(task_id),
        {"type": "approval", "gate_id": str(gate.id), "status": gate.status.value},
    )
    dispatcher.poke()
    return gate
