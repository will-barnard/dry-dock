"""Dispatch loop — matches QUEUED tasks to idle workers.

Runs on a tick (and is poked whenever a worker becomes idle or a task is
queued). Picks the highest-priority queued task per pool, finds a worker that
satisfies its capability needs, marks the task CLAIMED, creates a Run row, and
ships the job over the worker's WebSocket as a `claim_grant` message.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import Project, Run, Task, TaskStatus
from app.orchestrator.event_bus import bus
from app.orchestrator.pools import KNOWN_POOLS
from app.orchestrator.protocol import ClaimGrantMsg
from app.orchestrator.router import select_worker_for_task

log = structlog.get_logger()


async def _next_queued_for_pool(session: AsyncSession, pool: str) -> Task | None:
    stmt = (
        select(Task)
        .where(Task.required_pool == pool, Task.status == TaskStatus.QUEUED)
        .order_by(Task.priority.desc(), Task.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _dispatch_one_for_pool(pool: str) -> bool:
    """Attempt to dispatch one task for the given pool. Returns True on success."""
    async with SessionLocal() as session:
        async with session.begin():
            task = await _next_queued_for_pool(session, pool)
            if task is None:
                return False

            worker = await select_worker_for_task(task)
            if worker is None:
                # No live worker in the right pool with matching capabilities.
                return False

            project = await session.get(Project, task.project_id)
            assert project is not None

            task.status = TaskStatus.CLAIMED
            task.attempt += 1

            # Only pass a specific model to the worker when the task explicitly
            # sets one. When preferred_model is None the worker falls back to
            # its own DEFAULT_MODEL env var — right for heterogeneous pools
            # where each machine runs different local models.
            effective_model = task.preferred_model or None

            run = Run(
                id=uuid.uuid4(),
                task_id=task.id,
                worker_id=worker.worker_id,
                attempt=task.attempt,
                status=TaskStatus.CLAIMED,
                started_at=datetime.now(timezone.utc),
                worker_name=worker.name,
                model_used=effective_model,
            )
            session.add(run)
            await session.flush()

            grant = ClaimGrantMsg(
                task_id=task.id,
                run_id=run.id,
                kind=task.kind.value,
                title=task.title,
                prompt=task.prompt,
                required_pool=task.required_pool,
                branch_name=task.branch_name,
                preferred_model=effective_model,
                project={
                    "slug": project.slug,
                    "github_owner": project.github_owner,
                    "github_repo": project.github_repo,
                    "default_branch": project.default_branch,
                    "system_prompt": project.system_prompt,
                    "validate_commands": list(project.validate_commands or []),
                },
                payload=task.payload or {},
            )

        # Send outside the DB transaction.
        worker.current_task_id = task.id
        try:
            await worker.send(grant.model_dump(mode="json"))
        except Exception as exc:
            log.warning("dispatch.send_failed", worker=worker.name, error=str(exc))
            # Roll back the claim so another worker can pick it up.
            async with SessionLocal() as s2:
                async with s2.begin():
                    t2 = await s2.get(Task, task.id)
                    if t2:
                        t2.status = TaskStatus.QUEUED
            worker.current_task_id = None
            return False

        await bus.publish(
            bus.task_topic(task.id),
            {"type": "status", "task_id": str(task.id), "status": TaskStatus.CLAIMED.value},
        )
        log.info("dispatch.granted", task=str(task.id), pool=pool, worker=worker.name)
        return True


class Dispatcher:
    def __init__(self) -> None:
        self._tick = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def poke(self) -> None:
        self._tick.set()

    async def _loop(self) -> None:
        log.info("dispatcher.started")
        while not self._stop.is_set():
            try:
                # Try every pool on each tick. Pools without workers or without
                # queued tasks just return False quickly.
                for pool in KNOWN_POOLS:
                    while await _dispatch_one_for_pool(pool):
                        pass
            except Exception as exc:
                log.exception("dispatcher.loop_error", error=str(exc), error_type=type(exc).__name__)
            try:
                await asyncio.wait_for(self._tick.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            self._tick.clear()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="dispatcher")

    async def stop(self) -> None:
        self._stop.set()
        self._tick.set()
        if self._task:
            await self._task


dispatcher = Dispatcher()
