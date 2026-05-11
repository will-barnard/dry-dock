"""Project CRUD endpoints."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Project
from app.orchestrator.git_service import ensure_clone
from app.schemas import ProjectCreate, ProjectOut

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
async def list_projects(session: AsyncSession = Depends(get_session)) -> list[Project]:
    result = await session.execute(select(Project).order_by(Project.created_at.desc()))
    return list(result.scalars().all())


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    body: ProjectCreate, session: AsyncSession = Depends(get_session)
) -> Project:
    exists = await session.execute(select(Project).where(Project.slug == body.slug))
    if exists.scalar_one_or_none():
        raise HTTPException(409, "project slug already exists")
    project = Project(**body.model_dump())
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
