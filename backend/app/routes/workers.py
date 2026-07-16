"""Worker WebSocket endpoint + read-only HTTP listing."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal, get_session
from app.models import (
    ApprovalGate,
    ApprovalGateKind,
    Artifact,
    Event,
    Project,
    Run,
    Task,
    TaskKind,
    TaskStatus,
    Worker,
    WorkerStatus,
)
from app.orchestrator.dispatcher import dispatcher
from app.orchestrator.event_bus import bus
from app.orchestrator.git_service import apply_patch_and_push, open_pull_request
from app.orchestrator.lifecycle import promote_ready_children
from app.orchestrator.planner import materialize_plan
from app.orchestrator.chat import (
    handle_tool_call as chat_handle_tool_call,
    on_chunk as chat_on_chunk,
    on_done as chat_on_done,
    on_error as chat_on_error,
)
from app.orchestrator.protocol import (
    ArtifactMsg,
    ChatChunkMsg,
    ChatDoneMsg,
    ChatErrorMsg,
    ChatToolCallMsg,
    ChatToolResultMsg,
    ClaimRequestMsg,
    ErrorMsg,
    HeartbeatMsg,
    JobStartedMsg,
    LogChunkMsg,
    RegisterMsg,
    ResultMsg,
    WelcomeMsg,
    WorkbenchResultMsg,
)
from app.orchestrator.generate import fail_generate_jobs, resolve_generate
from app.orchestrator.workbench_jobs import (
    fail_jobs_on_worker_disconnect,
    handle_cover_letter_result,
    handle_import_result,
    handle_improve_result,
    handle_tag_bullets_result,
    handle_tailor_result,
)
from app.orchestrator.registry import LiveWorker, registry
from app.schemas import WorkerOut

log = structlog.get_logger()
router = APIRouter()

http_router = APIRouter(prefix="/api/workers", tags=["workers"])


@http_router.get("", response_model=list[WorkerOut])
async def list_workers(session: AsyncSession = Depends(get_session)) -> list[Worker]:
    result = await session.execute(select(Worker).order_by(Worker.name))
    return list(result.scalars().all())


# Map the first inbound message to a RegisterMsg.
_INBOUND_BY_TYPE = {
    "register": RegisterMsg,
    "heartbeat": HeartbeatMsg,
    "claim_request": ClaimRequestMsg,
    "job_started": JobStartedMsg,
    "log": LogChunkMsg,
    "artifact": ArtifactMsg,
    "result": ResultMsg,
    "error": ErrorMsg,
    "chat_chunk": ChatChunkMsg,
    "chat_done": ChatDoneMsg,
    "chat_error": ChatErrorMsg,
    "chat_tool_call": ChatToolCallMsg,
    "workbench_result": WorkbenchResultMsg,
}


def _parse_inbound(raw: dict):
    cls = _INBOUND_BY_TYPE.get(raw.get("type", ""))
    if cls is None:
        raise ValueError(f"unknown message type: {raw.get('type')}")
    return cls.model_validate(raw)


async def _upsert_worker_row(reg: RegisterMsg) -> Worker:
    async with SessionLocal() as session:
        async with session.begin():
            existing = await session.execute(select(Worker).where(Worker.name == reg.name))
            worker = existing.scalar_one_or_none()
            if worker is None:
                worker = Worker(
                    id=uuid.uuid4(),
                    name=reg.name,
                    pool=reg.pool,
                    hostname=reg.hostname,
                    hardware_class=reg.hardware_class,
                    ram_gb=reg.ram_gb,
                    installed_models=reg.installed_models,
                    max_context=reg.max_context,
                    gpu_vram_gb=reg.gpu_vram_gb,
                    gpu_model=reg.gpu_model,
                    status=WorkerStatus.ONLINE,
                    last_heartbeat=datetime.now(timezone.utc),
                    connected_at=datetime.now(timezone.utc),
                    metadata_blob=reg.metadata,
                )
                session.add(worker)
            else:
                worker.pool = reg.pool
                worker.hostname = reg.hostname
                worker.hardware_class = reg.hardware_class
                worker.ram_gb = reg.ram_gb
                worker.installed_models = reg.installed_models
                worker.max_context = reg.max_context
                worker.gpu_vram_gb = reg.gpu_vram_gb
                worker.gpu_model = reg.gpu_model
                worker.status = WorkerStatus.ONLINE
                worker.last_heartbeat = datetime.now(timezone.utc)
                worker.connected_at = datetime.now(timezone.utc)
                worker.metadata_blob = reg.metadata
        await session.refresh(worker)
        return worker


async def _persist_event(run_id: uuid.UUID, kind: str, stream: str, body: str) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            session.add(Event(run_id=run_id, kind=kind, stream=stream, body=body))


async def _handle_result(
    msg: ResultMsg, live: LiveWorker
) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            task = await session.get(Task, msg.task_id)
            run = await session.get(Run, msg.run_id)
            project = await session.get(Project, task.project_id) if task else None
            if not task or not run or not project:
                log.warning("result.missing_records", task=str(msg.task_id))
                return

            run.status = TaskStatus.SUCCEEDED if msg.success else TaskStatus.FAILED
            run.finished_at = datetime.now(timezone.utc)
            run.tokens_in = msg.tokens_in
            run.tokens_out = msg.tokens_out
            # Record the model the worker actually used (sent back in the result).
            if msg.model_used and not run.model_used:
                run.model_used = msg.model_used
            # Store the failure reason so it shows up in the UI.
            if not msg.success and not run.error:
                run.error = msg.summary

            if msg.success:
                # If the task produced a patch artifact, try to apply + open PR.
                patches = await session.execute(
                    select(Artifact).where(
                        Artifact.task_id == task.id, Artifact.kind == "patch"
                    ).order_by(Artifact.created_at.desc()).limit(1)
                )
                patch_art = patches.scalar_one_or_none()
                if patch_art:
                    try:
                        branch, is_new_branch = await apply_patch_and_push(
                            project, task, patch_art.content
                        )
                        task.branch_name = branch
                        # Only open a new PR when we just created the branch.
                        # Chained tasks push onto the same branch as their
                        # parent and share its PR — pushing more commits will
                        # update the existing PR automatically on GitHub.
                        if is_new_branch:
                            pr_url = await open_pull_request(
                                project, task, branch,
                                body=(
                                    f"Auto-generated by dry-dock task `{task.id}`."
                                    f"\n\n{msg.summary}"
                                ),
                            )
                            if pr_url:
                                task.payload = {
                                    **(task.payload or {}),
                                    "pull_request_url": pr_url,
                                }
                        else:
                            # Inherit the PR URL from the parent so the UI can
                            # link to it from chained tasks too.
                            parent = (
                                await session.get(Task, task.parent_task_id)
                                if task.parent_task_id else None
                            )
                            parent_pr = (
                                parent.payload.get("pull_request_url")
                                if parent and parent.payload else None
                            )
                            if parent_pr:
                                task.payload = {
                                    **(task.payload or {}),
                                    "pull_request_url": parent_pr,
                                }
                    except Exception as exc:
                        log.exception("result.patch_apply_failed", task=str(task.id))
                        run.error = f"patch apply failed: {exc}"
                        run.status = TaskStatus.FAILED
                        task.status = TaskStatus.FAILED
                        msg.success = False

                if msg.success:
                    # Plan tasks need approval before fan-out; code tasks need
                    # approval before merge (when gated). direct_push projects
                    # skip all gates — code is already on main.
                    if task.kind.value == "plan" and not project.auto_approve_plans:
                        gate = ApprovalGate(
                            task_id=task.id,
                            kind=ApprovalGateKind.PLAN,
                            payload={"summary": msg.summary, "result_payload": msg.payload},
                        )
                        session.add(gate)
                        task.status = TaskStatus.AWAITING_APPROVAL
                    elif task.branch_name and not project.auto_approve_merges and not project.direct_push:
                        gate = ApprovalGate(
                            task_id=task.id,
                            kind=ApprovalGateKind.MERGE,
                            payload={"summary": msg.summary, "branch": task.branch_name,
                                     "pull_request_url": task.payload.get("pull_request_url") if task.payload else None},
                        )
                        session.add(gate)
                        task.status = TaskStatus.AWAITING_APPROVAL
                        # The code is written — promote dependents now so they
                        # can work from this branch while the merge is reviewed.
                        await promote_ready_children(
                            session, task, parent_branch=task.branch_name
                        )
                    else:
                        task.status = TaskStatus.SUCCEEDED
                        task.result = {"summary": msg.summary, **msg.payload}
                        if task.kind.value == "plan":
                            await materialize_plan(session, task)
                        await promote_ready_children(
                            session, task, parent_branch=task.branch_name or None
                        )

            if not msg.success:
                task.status = (
                    TaskStatus.FAILED if task.attempt >= task.max_attempts else TaskStatus.QUEUED
                )
                # Validator-driven auto-requeue: a failed validator means the
                # work its parent produced is broken. Append the failure log
                # to the parent's prompt and re-queue the parent, then reset
                # this validator to PENDING so the chain promotes it again
                # when the parent re-succeeds. Capped by parent.max_attempts.
                if (
                    task.kind == TaskKind.VALIDATE
                    and task.parent_task_id is not None
                ):
                    parent = await session.get(Task, task.parent_task_id)
                    if (
                        parent is not None
                        and parent.kind in (TaskKind.CODE, TaskKind.REFACTOR, TaskKind.TEST, TaskKind.DOCS)
                        and parent.attempt < parent.max_attempts
                    ):
                        failure_block = (
                            "\n\n## Previous attempt failed validation\n"
                            "The validator that ran after your last attempt reported:\n\n"
                            f"```\n{msg.summary}\n```\n\n"
                            "Fix the issues above. The contract has not changed."
                        )
                        parent.prompt = (parent.prompt or "") + failure_block
                        parent.attempt += 1
                        parent.status = TaskStatus.QUEUED
                        parent.result = None
                        # Re-arm this validator so it runs again post-fix.
                        task.status = TaskStatus.PENDING
                        log.info(
                            "validator.auto_requeued_parent",
                            validator=str(task.id), parent=str(parent.id),
                            parent_attempt=parent.attempt,
                        )

    live.current_task_id = None
    await bus.publish(
        bus.task_topic(msg.task_id),
        {"type": "status", "task_id": str(msg.task_id),
         "status": TaskStatus.SUCCEEDED.value if msg.success else "failed"},
    )
    dispatcher.poke()


@router.websocket("/ws/worker")
async def worker_socket(ws: WebSocket, token: str = Query(...)):
    settings = get_settings()
    if token != settings.worker_shared_secret:
        await ws.close(code=4401)
        return

    await ws.accept()

    # First message MUST be register.
    raw = await ws.receive_json()
    try:
        reg = RegisterMsg.model_validate(raw)
    except ValidationError as exc:
        await ws.send_json(ErrorMsg(code="bad_register", message=str(exc), retryable=False).model_dump(mode="json"))
        await ws.close(code=4400)
        return

    worker_row = await _upsert_worker_row(reg)
    live = LiveWorker(
        worker_id=worker_row.id,
        name=reg.name,
        pool=reg.pool,
        socket=ws,
        installed_models=reg.installed_models,
        max_context=reg.max_context,
        ram_gb=reg.ram_gb,
        gpu_vram_gb=reg.gpu_vram_gb,
        gpu_model=reg.gpu_model,
        hardware_class=reg.hardware_class,
    )
    await registry.add(live)
    await live.send(
        WelcomeMsg(worker_id=worker_row.id, server_version="0.1.0").model_dump(mode="json")
    )
    log.info("worker.connected", name=reg.name, pool=reg.pool)
    dispatcher.poke()

    try:
        while True:
            raw = await ws.receive_json()
            try:
                msg = _parse_inbound(raw)
            except (ValueError, ValidationError) as exc:
                log.warning("worker.bad_message", name=reg.name, error=str(exc))
                continue

            if isinstance(msg, HeartbeatMsg):
                async with SessionLocal() as session:
                    async with session.begin():
                        w = await session.get(Worker, worker_row.id)
                        if w:
                            w.last_heartbeat = datetime.now(timezone.utc)
                            w.status = (
                                WorkerStatus.BUSY if msg.current_task_id else WorkerStatus.ONLINE
                            )
            elif isinstance(msg, ClaimRequestMsg):
                # Worker self-reported idle. Make sure runtime view agrees and poke dispatcher.
                live.current_task_id = None
                dispatcher.poke()
            elif isinstance(msg, JobStartedMsg):
                async with SessionLocal() as session:
                    async with session.begin():
                        run = await session.get(Run, msg.run_id)
                        task = await session.get(Task, msg.task_id)
                        if run:
                            run.status = TaskStatus.RUNNING
                        if task:
                            task.status = TaskStatus.RUNNING
                await bus.publish(
                    bus.task_topic(msg.task_id),
                    {"type": "status", "task_id": str(msg.task_id), "status": "running"},
                )
            elif isinstance(msg, LogChunkMsg):
                await _persist_event(msg.run_id, "log", msg.stream, msg.body)
                await bus.publish(
                    bus.task_topic(msg.task_id),
                    {"type": "log", "stream": msg.stream, "body": msg.body},
                )
            elif isinstance(msg, ArtifactMsg):
                async with SessionLocal() as session:
                    async with session.begin():
                        session.add(Artifact(
                            task_id=msg.task_id,
                            run_id=msg.run_id,
                            kind=msg.kind,
                            name=msg.name,
                            content=msg.content,
                            metadata_blob=msg.metadata,
                        ))
                await bus.publish(
                    bus.task_topic(msg.task_id),
                    {"type": "artifact", "kind": msg.kind, "name": msg.name},
                )
            elif isinstance(msg, ResultMsg):
                await _handle_result(msg, live)
            elif isinstance(msg, ErrorMsg):
                log.warning("worker.error_msg", code=msg.code, message=msg.message)
                if msg.task_id and msg.run_id:
                    await _persist_event(msg.run_id, "error", "system", msg.message)
            elif isinstance(msg, ChatChunkMsg):
                await chat_on_chunk(msg.conversation_id, msg.assistant_message_id, msg.delta)
            elif isinstance(msg, ChatDoneMsg):
                await chat_on_done(msg.conversation_id, msg.assistant_message_id, msg.content)
            elif isinstance(msg, ChatErrorMsg):
                await chat_on_error(msg.conversation_id, msg.assistant_message_id, msg.error)
            elif isinstance(msg, ChatToolCallMsg):
                # Agentic mode: the model requested a tool. Run it and ship
                # the result back so the worker's loop can continue. Failures
                # come back as success=False with an error string the model
                # can react to rather than crashing the turn.
                try:
                    result = await chat_handle_tool_call(
                        msg.conversation_id, msg.assistant_message_id,
                        msg.tool_call_id, msg.name, msg.arguments,
                    )
                    reply = ChatToolResultMsg(
                        conversation_id=msg.conversation_id,
                        assistant_message_id=msg.assistant_message_id,
                        tool_call_id=msg.tool_call_id,
                        success=bool(result.get("success")),
                        content=str(result.get("content") or ""),
                    )
                except Exception as exc:
                    log.exception("worker.tool_call_failed", tool=msg.name)
                    reply = ChatToolResultMsg(
                        conversation_id=msg.conversation_id,
                        assistant_message_id=msg.assistant_message_id,
                        tool_call_id=msg.tool_call_id,
                        success=False, content="", error=str(exc),
                    )
                try:
                    await live.send(reply.model_dump(mode="json"))
                except Exception:
                    log.exception("worker.tool_result_send_failed")
            elif isinstance(msg, WorkbenchResultMsg):
                # Clear in-flight tracking so the disconnect path doesn't try
                # to fail an already-completed job if the socket drops next.
                live.current_workbench_jobs.discard(msg.job_id)
                # Generate-API jobs are correlated to a waiting HTTP request via
                # an in-memory Future rather than a DB handler. resolve_generate
                # returns True if it owned this job_id, so we stop here.
                if resolve_generate(msg.job_id, msg.success, msg.content, msg.error):
                    pass
                # One handler per Workbench job kind.
                elif msg.kind == "import":
                    await handle_import_result(
                        msg.job_id, msg.success, msg.content, msg.error
                    )
                elif msg.kind == "tailor":
                    await handle_tailor_result(
                        msg.job_id, msg.success, msg.content, msg.error
                    )
                elif msg.kind == "improve":
                    await handle_improve_result(
                        msg.job_id, msg.success, msg.content, msg.error
                    )
                elif msg.kind == "cover_letter":
                    await handle_cover_letter_result(
                        msg.job_id, msg.success, msg.content, msg.error
                    )
                elif msg.kind == "tag_bullets":
                    await handle_tag_bullets_result(
                        msg.job_id, msg.success, msg.content, msg.error
                    )
                elif msg.kind == "scout_learn":
                    from app.orchestrator.scout import handle_site_learning_result
                    await handle_site_learning_result(
                        msg.job_id, msg.success, msg.content, msg.error
                    )
                else:
                    log.warning("worker.unknown_workbench_kind", kind=msg.kind)

    except WebSocketDisconnect:
        log.info("worker.disconnected", name=reg.name)
    except Exception:
        log.exception("worker.socket_error", name=reg.name)
    finally:
        await registry.remove(worker_row.id)
        async with SessionLocal() as session:
            async with session.begin():
                w = await session.get(Worker, worker_row.id)
                if w:
                    w.status = WorkerStatus.OFFLINE
        # If the worker died mid-task, requeue.
        if live.current_task_id is not None:
            async with SessionLocal() as session:
                async with session.begin():
                    t = await session.get(Task, live.current_task_id)
                    if t and t.status in (TaskStatus.CLAIMED, TaskStatus.RUNNING):
                        t.status = TaskStatus.QUEUED if t.attempt < t.max_attempts else TaskStatus.FAILED
            dispatcher.poke()
        # Fail any Workbench jobs that were in flight on this worker. They
        # don't have a retry path like Tasks do, so the safest thing is to
        # surface ERROR so the user knows the job died and can retry.
        if live.current_workbench_jobs:
            stranded = set(live.current_workbench_jobs)
            # Reject any waiting generate-API HTTP requests first (in-memory
            # futures — no DB row). Non-generate ids are ignored here.
            try:
                fail_generate_jobs(stranded, reg.name)
            except Exception:
                log.exception("worker.generate_disconnect_handler_failed")
            # Then surface any DB-backed Workbench jobs as ERROR.
            try:
                await fail_jobs_on_worker_disconnect(stranded, reg.name)
            except Exception:
                log.exception("worker.workbench_disconnect_handler_failed")
