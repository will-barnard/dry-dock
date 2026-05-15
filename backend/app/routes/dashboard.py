"""HTMX dashboard views — server-rendered HTML."""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
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
    User,
    Worker,
)
from app.orchestrator.dispatcher import dispatcher
from app.orchestrator.lifecycle import (
    DELETABLE_STATUSES,
    delete_project as do_delete_project,
    delete_task as do_delete_task,
    reopen_approval as do_reopen_approval,
    requeue_failed_children as do_requeue_failed_children,
    rerun_task as do_rerun_task,
)
from app.orchestrator.planner import materialize_plan
from app.orchestrator.pools import pool_for_kind
from app.orchestrator.registry import registry
from app.util.github import normalize_owner_repo, parse_github_ref

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── Modules ────────────────────────────────────────────────────────
#
# dry-dock is becoming a multi-module platform: the same orchestrator + worker
# fleet powers several distinct tools. Each module is a top-level area of the
# app with its own route. The homepage is now a module picker.
#
#   engineer  — the original product: autonomous multi-agent software builds
#   operator  — a chat surface that talks to the worker fleet directly
#   workbench — a resume + cover-letter authoring tool
#
# `status`: "active" modules are fully built; "preview" modules have a route
# and a stub page but no real functionality yet.

MODULES: list[dict] = [
    {
        "id": "engineer",
        "name": "Engineer",
        "href": "/engineer",
        "status": "active",
        "tagline": "Autonomous multi-agent software builds",
        "description": (
            "Point it at a GitHub repo, describe a goal, and a planner fans the "
            "work out across coder / reviewer / tester / refactorer / docs / "
            "validator pools. Approval gates, contracts, and a live task DAG."
        ),
        "accent": "sky",
    },
    {
        "id": "operator",
        "name": "Operator",
        "href": "/operator",
        "status": "preview",
        "tagline": "Chat directly with the worker fleet",
        "description": (
            "A conversational surface over the same Ollama-backed workers — ask "
            "questions, run one-off research or summarization jobs, no project "
            "or repo required."
        ),
        "accent": "violet",
    },
    {
        "id": "workbench",
        "name": "Workbench",
        "href": "/workbench",
        "status": "preview",
        "tagline": "Resume & cover-letter authoring",
        "description": (
            "Draft, tailor, and iterate on resumes and cover letters against a "
            "specific job posting, with the worker fleet doing the heavy "
            "drafting and revision passes."
        ),
        "accent": "amber",
    },
]


def _module(module_id: str) -> dict | None:
    return next((m for m in MODULES if m["id"] == module_id), None)


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Module picker — the new front door."""
    # A light status line per module. Only Engineer has real counts today.
    project_count = (await session.execute(
        select(func.count()).select_from(Project)
    )).scalar_one()
    live_count = len(await registry.all())
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": user,
            "modules": MODULES,
            "engineer_stats": {
                "projects": project_count,
                "workers_online": live_count,
            },
        },
    )


@router.get("/engineer", response_class=HTMLResponse)
async def engineer(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """The Engineer module — what used to be the homepage."""
    projects = list((await session.execute(
        select(Project).order_by(Project.created_at.desc())
    )).scalars().all())
    live = await registry.all()
    workers = list((await session.execute(select(Worker).order_by(Worker.name))).scalars().all())
    return templates.TemplateResponse(
        request,
        "engineer.html",
        {"user": user, "projects": projects, "workers": workers, "live_count": len(live)},
    )


# NOTE: /operator and its sub-routes now live in routes/operator.py — the
# Operator module is built out, no longer a stub.


@router.get("/workbench", response_class=HTMLResponse)
async def workbench(
    request: Request, user: User = Depends(get_current_user)
) -> HTMLResponse:
    """Workbench module — resume & cover-letter tool. Stub for now."""
    return templates.TemplateResponse(
        request, "workbench.html", {"user": user, "module": _module("workbench")},
    )


@router.post("/projects", response_class=HTMLResponse)
async def create_project(
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    github_url: str = Form(...),
    default_branch: str = Form("main"),
    system_prompt: str = Form(""),
    auto_approve_plans: bool = Form(False),
    auto_approve_merges: bool = Form(False),
    session: AsyncSession = Depends(get_session),
):
    # Accept any of: owner/repo, https://github.com/owner/repo[.git],
    # git@github.com:owner/repo.git — parse into separate fields.
    parsed = parse_github_ref(github_url)
    if not parsed:
        raise HTTPException(
            400,
            "Couldn't parse GitHub repo. Try `owner/repo` or a full GitHub URL.",
        )
    owner, repo = parsed

    existing = await session.execute(select(Project).where(Project.slug == slug))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "slug already exists")
    project = Project(
        slug=slug,
        name=name,
        github_owner=owner,
        github_repo=repo,
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
    request: Request,
    project_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
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
            "user": user,
            "project": project,
            "tasks": tasks,
            "pending_approvals": pending_approvals,
            "task_kinds": [k.value for k in TaskKind],
            "validate_commands_text": "\n".join(project.validate_commands or []),
        },
    )


@router.post("/projects/{project_id}/validate-commands", response_class=HTMLResponse)
async def update_validate_commands(
    request: Request,
    project_id: uuid.UUID,
    validate_commands: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")
    # One command per line, blanks dropped, leading/trailing whitespace trimmed.
    commands = [line.strip() for line in (validate_commands or "").splitlines() if line.strip()]
    project.validate_commands = commands
    await session.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/settings", response_class=HTMLResponse)
async def update_project_settings(
    request: Request,
    project_id: uuid.UUID,
    auto_approve_plans: bool = Form(False),
    auto_approve_merges: bool = Form(False),
    direct_push: bool = Form(False),
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")
    project.auto_approve_plans = auto_approve_plans
    project.auto_approve_merges = auto_approve_merges
    project.direct_push = direct_push
    await session.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/tasks", response_class=HTMLResponse)
async def create_task_form(
    request: Request,
    project_id: uuid.UUID,
    kind: str = Form(...),
    title: str = Form(...),
    prompt: str = Form(...),
    required_pool: str = Form(""),
    preferred_model: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")
    task_kind = TaskKind(kind)
    # Empty required_pool means "use the canonical pool for this kind". This
    # is what most users want; the field stays available for the rare case
    # where you want to route a, say, code task to the reviewer pool.
    pool = (required_pool or "").strip() or pool_for_kind(task_kind)
    task = Task(
        project_id=project_id,
        kind=task_kind,
        title=title,
        prompt=prompt,
        required_pool=pool,
        preferred_model=preferred_model or None,
        status=TaskStatus.QUEUED,
    )
    session.add(task)
    await session.commit()
    dispatcher.poke()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(
    request: Request,
    task_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
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
            "user": user,
            "task": task,
            "project": project,
            "runs": runs,
            "artifacts": artifacts,
            "approvals": approvals,
            "events": events,
        },
    )


# ── delete / re-run actions ────────────────────────────────────────


@router.post("/tasks/{task_id}/delete", response_class=HTMLResponse)
async def delete_task_form(
    task_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "task not found")
    if task.status not in DELETABLE_STATUSES:
        raise HTTPException(409, f"task is {task.status.value} — cancel it first")
    project_id = task.project_id
    await do_delete_task(session, task, cascade=True)
    await session.commit()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/tasks/{task_id}/enqueue", response_class=HTMLResponse)
async def enqueue_task_form(
    task_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "task not found")
    if task.status != TaskStatus.PENDING:
        raise HTTPException(409, f"task is {task.status.value}, not pending")
    task.status = TaskStatus.QUEUED
    await session.commit()
    dispatcher.poke()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/rerun", response_class=HTMLResponse)
async def rerun_task_form(
    task_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "task not found")
    try:
        await do_rerun_task(session, task)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    await session.commit()
    dispatcher.poke()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/rerun-failed-children", response_class=HTMLResponse)
async def requeue_children_form(
    task_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "task not found")
    await do_requeue_failed_children(session, task)
    await session.commit()
    dispatcher.poke()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/projects/{project_id}/delete", response_class=HTMLResponse)
async def delete_project_form(
    project_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")
    await do_delete_project(session, project)
    await session.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/approvals/{gate_id}/reopen", response_class=HTMLResponse)
async def reopen_approval_form(
    gate_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    gate = await session.get(ApprovalGate, gate_id)
    if not gate:
        raise HTTPException(404, "approval gate not found")
    await do_reopen_approval(session, gate)
    await session.commit()
    return RedirectResponse(f"/tasks/{gate.task_id}", status_code=303)


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
