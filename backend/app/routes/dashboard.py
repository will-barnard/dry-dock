"""HTMX dashboard views — server-rendered HTML."""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import (
    ApprovalGate,
    ApprovalGateKind,
    ApprovalGateStatus,
    Artifact,
    Event,
    Project,
    Run,
    Task,
    TaskKind,
    TaskStatus,
    Worker,
)
from app.orchestrator.dispatcher import dispatcher
from app.orchestrator.planner import materialize_plan
from app.orchestrator.registry import registry

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    projects = list((await session.execute(
        select(Project).order_by(Project.created_at.desc())
    )).scalars().all())
    live = await registry.all()
    workers = list((await session.execute(select(Worker).order_by(Worker.name))).scalars().all())
    return templates.TemplateResponse(
        request,
        "index.html",
        {"projects": projects, "workers": workers, "live_count": len(live)},
    )


@router.post("/projects", response_class=HTMLResponse)
async def create_project(
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    github_owner: str = Form(...),
    github_repo: str = Form(...),
    default_branch: str = Form("main"),
    system_prompt: str = Form(""),
    auto_approve_plans: bool = Form(False),
    auto_approve_merges: bool = Form(False),
    session: AsyncSession = Depends(get_session),
):
    existing = await session.execute(select(Project).where(Project.slug == slug))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "slug already exists")
    project = Project(
        slug=slug,
        name=name,
        github_owner=github_owner,
        github_repo=github_repo,
        default_branch=default_branch,
        system_prompt=system_prompt or None,
        auto_approve_plans=auto_approve_plans,
        auto_approve_merges=auto_approve_merges,
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(
    request: Request, project_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")
    tasks = list((await session.execute(
        select(Task).where(Task.project_id == project_id).order_by(desc(Task.created_at))
    )).scalars().all())
    pending_approvals = list((await session.execute(
        select(ApprovalGate)
        .join(Task, Task.id == ApprovalGate.task_id)
        .where(Task.project_id == project_id, ApprovalGate.status == ApprovalGateStatus.PENDING)
        .order_by(ApprovalGate.created_at.desc())
    )).scalars().all())
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "project": project,
            "tasks": tasks,
            "pending_approvals": pending_approvals,
            "task_kinds": [k.value for k in TaskKind],
        },
    )


@router.post("/projects/{project_id}/tasks", response_class=HTMLResponse)
async def create_task_form(
    request: Request,
    project_id: uuid.UUID,
    kind: str = Form(...),
    title: str = Form(...),
    prompt: str = Form(...),
    required_pool: str = Form(...),
    preferred_model: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")
    task = Task(
        project_id=project_id,
        kind=TaskKind(kind),
        title=title,
        prompt=prompt,
        required_pool=required_pool,
        preferred_model=preferred_model or None,
        status=TaskStatus.QUEUED,
    )
    session.add(task)
    await session.commit()
    dispatcher.poke()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(
    request: Request, task_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "task not found")
    project = await session.get(Project, task.project_id)
    runs = list((await session.execute(
        select(Run).where(Run.task_id == task_id).order_by(desc(Run.started_at))
    )).scalars().all())
    artifacts = list((await session.execute(
        select(Artifact).where(Artifact.task_id == task_id).order_by(desc(Artifact.created_at))
    )).scalars().all())
    approvals = list((await session.execute(
        select(ApprovalGate).where(ApprovalGate.task_id == task_id).order_by(desc(ApprovalGate.created_at))
    )).scalars().all())
    events = []
    if runs:
        events = list((await session.execute(
            select(Event).where(Event.run_id == runs[0].id).order_by(Event.ts.asc()).limit(500)
        )).scalars().all())
    return templates.TemplateResponse(
        request,
        "task.html",
        {
            "task": task,
            "project": project,
            "runs": runs,
            "artifacts": artifacts,
            "approvals": approvals,
            "events": events,
        },
    )


@router.post("/approvals/{gate_id}", response_class=HTMLResponse)
async def decide_approval_form(
    request: Request,
    gate_id: uuid.UUID,
    decision: str = Form(...),
    note: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    from datetime import datetime, timezone

    gate = await session.get(ApprovalGate, gate_id)
    if not gate:
        raise HTTPException(404, "gate not found")
    task = await session.get(Task, gate.task_id)
    if not task:
        raise HTTPException(404, "task not found")

    approve = decision == "approve"
    gate.status = ApprovalGateStatus.APPROVED if approve else ApprovalGateStatus.REJECTED
    gate.note = note or None
    gate.decided_at = datetime.now(timezone.utc)
    gate.decided_by = "user"

    if gate.kind == ApprovalGateKind.PLAN and approve:
        await materialize_plan(session, task)
        task.status = TaskStatus.SUCCEEDED
    elif gate.kind == ApprovalGateKind.MERGE and approve:
        task.status = TaskStatus.SUCCEEDED
    else:
        task.status = TaskStatus.REJECTED

    await session.commit()
    dispatcher.poke()
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)
