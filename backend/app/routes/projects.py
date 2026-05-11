"""Project CRUD endpoints."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Project
from app.orchestrator.git_service import ensure_clone
from app.orchestrator.lifecycle import delete_project as do_delete_project
from app.schemas import ProjectCreate, ProjectOut
from app.util.github import normalize_owner_repo

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
async def list_projects(session: AsyncSession = Depends(get_session)) -> list[Project]:
    result = await session.execute(select(Project).order_by(Project.created_at.desc()))
    return list(result.scalars().all())


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    body: ProjectCreate, session: AsyncSession = Depends(get_session)
) -> Project:
    # Accept any of the common ways callers might paste a GitHub ref. Same
    # parser the HTMX form uses — keeps behavior aligned across surfaces.
    parsed = normalize_owner_repo(body.github_owner, body.github_repo)
    if not parsed:
        raise HTTPException(
            400,
            "github_owner/github_repo couldn't be parsed. Pass owner+repo "
            "separately or a full GitHub URL in github_repo.",
        )
    owner, repo = parsed

    exists = await session.execute(select(Project).where(Project.slug == body.slug))
    if exists.scalar_one_or_none():
        raise HTTPException(409, "project slug already exists")
    data = body.model_dump()
    data["github_owner"] = owner
    data["github_repo"] = repo
    project = Project(**data)
    session.add(project)
    await session.commit()
    await session.refresh(project)

    # Best-effort clone — failures don't block project creation.
    try:
        await ensure_clone(project)
    except Exception:
        pass
    return project


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Project:
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")
    return project


@router.delete("/{project_id}", status_code=204, response_class=Response)
async def delete_project_api(
    project_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Response:
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")
    await do_delete_project(session, project)
    await session.commit()
    return Response(status_code=204)
