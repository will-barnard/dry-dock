"""Planner — given a high-level project goal, emit a sequence of child tasks.

The planner itself runs as a task (kind=PLAN) on the planner pool. Its
deliverable is a `plan` artifact: a JSON array of {kind, title, prompt,
required_pool, depends_on} entries. On approval, the orchestrator materializes
those entries as Task rows (with parent_task_id pointing back at the plan
task) and queues them.

This module exposes the server-side post-approval materialization. The
LLM-driven plan generation lives in the worker (`worker/app/runners/planner.py`).
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Artifact, Task, TaskKind, TaskStatus
from app.orchestrator.pools import KIND_TO_POOL

log = structlog.get_logger()


async def materialize_plan(session: AsyncSession, plan_task: Task) -> list[Task]:
    """Expand a plan artifact into child tasks. Caller commits."""
    stmt = (
        select(Artifact)
        .where(Artifact.task_id == plan_task.id, Artifact.kind == "summary")
        .order_by(Artifact.created_at.desc())
        .limit(1)
    )
    art = (await session.execute(stmt)).scalar_one_or_none()
    if art is None:
        log.warning("planner.no_plan_artifact", task=str(plan_task.id))
        return []

    try:
        entries: list[dict[str, Any]] = json.loads(art.content)
    except json.JSONDecodeError:
        log.warning("planner.plan_not_json", task=str(plan_task.id))
        return []

    # Map local index → real task id so we can wire up parent links by index.
    id_by_index: dict[int, uuid.UUID] = {}
    created: list[Task] = []

    for idx, entry in enumerate(entries):
        try:
            kind = TaskKind(entry["kind"])
        except (KeyError, ValueError):
            log.warning("planner.bad_kind", entry=entry)
            continue

        pool = entry.get("required_pool") or KIND_TO_POOL[kind]
        depends_on = entry.get("depends_on")
        parent_id: uuid.UUID | None = plan_task.id
        if isinstance(depends_on, int) and depends_on in id_by_index:
            parent_id = id_by_index[depends_on]

        new_task = Task(
            id=uuid.uuid4(),
            project_id=plan_task.project_id,
            parent_task_id=parent_id,
            kind=kind,
            title=entry.get("title", f"{kind.value} task"),
            prompt=entry.get("prompt", ""),
            required_pool=pool,
            min_ram_gb=int(entry.get("min_ram_gb", 0)),
            min_context=int(entry.get("min_context", 0)),
            preferred_model=entry.get("preferred_model"),
            payload=entry.get("payload") or {},
            status=TaskStatus.QUEUED,
        )
        session.add(new_task)
        await session.flush()
        id_by_index[idx] = new_task.id
        created.append(new_task)

    log.info("planner.materialized", plan_task=str(plan_task.id), count=len(created))
    return created
