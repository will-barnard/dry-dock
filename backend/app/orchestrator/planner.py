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
    """Expand a plan artifact into child tasks. Caller commits.

    Plan artifact format accepted:

      New (preferred):
        {
          "contract": "markdown invariants for this plan",
          "tasks": [ {kind, title, prompt, target_files?, depends_on?, ...} ]
        }

      Legacy (still parsed for backward compat):
        [ {kind, title, prompt, depends_on?, ...} ]

    The contract (when present) is copied verbatim into every child's
    ``payload.contract`` so downstream runners can prepend it to their
    prompts without having to fish it out of the planner's artifacts. The
    same goes for ``target_files``.
    """
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
        parsed = json.loads(art.content)
    except json.JSONDecodeError:
        log.warning("planner.plan_not_json", task=str(plan_task.id))
        return []

    if isinstance(parsed, list):
        entries: list[dict[str, Any]] = parsed
        contract = ""
    elif isinstance(parsed, dict):
        entries = parsed.get("tasks") or []
        contract = (parsed.get("contract") or "").strip()
        if not isinstance(entries, list):
            log.warning("planner.plan_tasks_not_list", task=str(plan_task.id))
            return []
    else:
        log.warning("planner.plan_unexpected_shape", task=str(plan_task.id))
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
        has_dep = isinstance(depends_on, int) and depends_on in id_by_index
        parent_id: uuid.UUID | None = plan_task.id
        if has_dep:
            parent_id = id_by_index[depends_on]

        # Build the payload: start from any caller-provided payload, then
        # layer in target_files and contract from the plan envelope.
        payload: dict[str, Any] = dict(entry.get("payload") or {})
        target_files = entry.get("target_files")
        if isinstance(target_files, list) and target_files:
            payload["target_files"] = [str(f) for f in target_files if isinstance(f, str)]
        if contract:
            payload["contract"] = contract

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
            payload=payload,
            # Tasks that depend on another plan step start PENDING and are
            # promoted to QUEUED only after their parent task succeeds.
            status=TaskStatus.PENDING if has_dep else TaskStatus.QUEUED,
        )
        session.add(new_task)
        await session.flush()
        id_by_index[idx] = new_task.id
        created.append(new_task)

    log.info(
        "planner.materialized",
        plan_task=str(plan_task.id), count=len(created), contract_chars=len(contract),
    )
    return created
