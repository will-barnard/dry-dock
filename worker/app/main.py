"""Worker entrypoint.

One long-lived WebSocket to the orchestrator. On connect: register and ask for
work. On receiving a claim_grant: spin up the right runner in a background task
so we can keep handling pings/heartbeats. When the runner finishes, send the
result and ask for the next job.

Single-job concurrency per worker is intentional: it keeps Ollama out of memory
contention, and parallelism comes from running multiple worker containers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import signal
import sys
import uuid
from typing import Any

import structlog
import websockets

from app.config import get_settings
from app.ollama_client import get_provider
from app.protocol import (
    ArtifactMsg,
    ChatChunkMsg,
    ChatDoneMsg,
    ChatErrorMsg,
    ChatRequestMsg,
    ClaimGrantMsg,
    ClaimRequestMsg,
    HeartbeatMsg,
    JobStartedMsg,
    LogChunkMsg,
    RegisterMsg,
    ResultMsg,
    WorkbenchRequestMsg,
    WorkbenchResultMsg,
)
from app.runners import RUNNERS, RunnerContext


def _configure_logging() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )


_configure_logging()
log = structlog.get_logger()


class Worker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.ws: websockets.WebSocketClientProtocol | None = None
        self.current_task: uuid.UUID | None = None
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()

    # ── transport ──────────────────────────────────────────────────────────

    async def send(self, payload: dict[str, Any]) -> None:
        assert self.ws is not None
        async with self._send_lock:
            await self.ws.send(json.dumps(payload, default=str))

    async def emit_log(self, task_id: uuid.UUID, run_id: uuid.UUID, stream: str, body: str) -> None:
        msg = LogChunkMsg(task_id=task_id, run_id=run_id, stream=stream, body=body)
        await self.send(msg.model_dump(mode="json"))

    async def emit_artifact(
        self,
        task_id: uuid.UUID,
        run_id: uuid.UUID,
        kind: str,
        name: str,
        content: str,
        metadata: dict[str, Any],
    ) -> None:
        msg = ArtifactMsg(
            task_id=task_id, run_id=run_id, kind=kind, name=name,
            content=content, metadata=metadata,
        )
        await self.send(msg.model_dump(mode="json"))

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def register(self) -> None:
        try:
            installed = await get_provider().list_models()
        except Exception as exc:
            log.warning("ollama.list_failed", error=str(exc))
            installed = []

        reg = RegisterMsg(
            name=self.settings.worker_name,
            pool=self.settings.worker_pool,
            hostname=platform.node(),
            hardware_class=self.settings.hardware_class,
            ram_gb=self.settings.ram_gb,
            installed_models=installed,
            max_context=self.settings.max_context,
            gpu_vram_gb=self.settings.gpu_vram_gb,
            gpu_model=self.settings.gpu_model,
            metadata={"platform": platform.platform()},
        )
        await self.send(reg.model_dump(mode="json"))
        log.info("worker.registered", pool=self.settings.worker_pool, models=installed)

        await self.send(ClaimRequestMsg().model_dump(mode="json"))

    async def heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                hb = HeartbeatMsg(current_task_id=self.current_task)
                await self.send(hb.model_dump(mode="json"))
            except Exception:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                pass

    async def run_job(self, grant: ClaimGrantMsg) -> None:
        runner_cls = RUNNERS.get(grant.required_pool)
        if runner_cls is None:
            log.error("worker.unknown_pool", pool=grant.required_pool)
            await self.send(ResultMsg(
                task_id=grant.task_id, run_id=grant.run_id, success=False,
                summary=f"worker has no runner for pool {grant.required_pool}",
            ).model_dump(mode="json"))
            self.current_task = None
            await self.send(ClaimRequestMsg().model_dump(mode="json"))
            return

        self.current_task = grant.task_id
        await self.send(JobStartedMsg(task_id=grant.task_id, run_id=grant.run_id).model_dump(mode="json"))

        async def _emit_log(stream: str, body: str) -> None:
            await self.emit_log(grant.task_id, grant.run_id, stream, body)

        async def _emit_artifact(kind: str, name: str, content: str, metadata: dict[str, Any]) -> None:
            await self.emit_artifact(grant.task_id, grant.run_id, kind, name, content, metadata)

        ctx = RunnerContext(
            task_id=str(grant.task_id),
            run_id=str(grant.run_id),
            title=grant.title,
            prompt=grant.prompt,
            project=grant.project,
            payload=grant.payload,
            preferred_model=grant.preferred_model,
            emit_log=_emit_log,
            emit_artifact=_emit_artifact,
            branch_name=grant.branch_name,
        )

        try:
            result = await runner_cls(ctx).run()
        except Exception as exc:
            log.exception("worker.runner_failed")
            await _emit_log("stderr", f"runner crashed: {exc}")
            result = type("R", (), {})()  # ad-hoc result
            result.success = False
            result.summary = f"runner crashed: {exc}"
            result.payload = {}
            result.tokens_in = 0
            result.tokens_out = 0

        await self.send(ResultMsg(
            task_id=grant.task_id,
            run_id=grant.run_id,
            success=result.success,
            summary=result.summary,
            payload=result.payload,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            model_used=ctx.preferred_model or self.settings.default_model,
        ).model_dump(mode="json"))

        self.current_task = None
        await self.send(ClaimRequestMsg().model_dump(mode="json"))

    async def run_chat(self, req: ChatRequestMsg) -> None:
        """Answer one Operator conversation turn by streaming Ollama output.

        Unlike run_job there's no clone, no runner class, no result row — we
        stream chat_chunk deltas and finish with chat_done (or chat_error).
        Runs concurrently with the consume loop, same as run_job.
        """
        provider = get_provider()
        model = req.model or self.settings.default_model
        log.info("worker.chat_started", conversation=str(req.conversation_id), model=model)
        chunks: list[str] = []
        tokens_in = 0
        tokens_out = 0
        try:
            async for ev in provider.chat_stream(model, req.messages):
                piece = (ev.get("message") or {}).get("content") or ""
                if piece:
                    chunks.append(piece)
                    await self.send(ChatChunkMsg(
                        conversation_id=req.conversation_id,
                        assistant_message_id=req.assistant_message_id,
                        delta=piece,
                    ).model_dump(mode="json"))
                if ev.get("done"):
                    tokens_in = ev.get("prompt_eval_count", 0) or 0
                    tokens_out = ev.get("eval_count", 0) or 0
        except Exception as exc:
            log.exception("worker.chat_failed")
            await self.send(ChatErrorMsg(
                conversation_id=req.conversation_id,
                assistant_message_id=req.assistant_message_id,
                error=str(exc),
            ).model_dump(mode="json"))
            return

        await self.send(ChatDoneMsg(
            conversation_id=req.conversation_id,
            assistant_message_id=req.assistant_message_id,
            content="".join(chunks),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        ).model_dump(mode="json"))
        log.info("worker.chat_done", conversation=str(req.conversation_id))

    async def run_workbench_job(self, req: WorkbenchRequestMsg) -> None:
        """Run one non-streaming Workbench inference (resume import, tailoring,
        bullet improvement). Unlike chat, there's no streaming — the
        orchestrator needs the whole (usually JSON) response to act on it, so
        we just call `provider.chat` once and return the full content."""
        provider = get_provider()
        model = req.model or self.settings.default_model
        log.info("worker.workbench_started", job=str(req.job_id), kind=req.kind, model=model)
        try:
            result = await provider.chat(model, req.messages)
            content = (result.get("message") or {}).get("content") or ""
            await self.send(WorkbenchResultMsg(
                job_id=req.job_id, kind=req.kind, success=True, content=content,
            ).model_dump(mode="json"))
            log.info("worker.workbench_done", job=str(req.job_id), kind=req.kind)
        except Exception as exc:
            log.exception("worker.workbench_failed")
            await self.send(WorkbenchResultMsg(
                job_id=req.job_id, kind=req.kind, success=False, content="", error=str(exc),
            ).model_dump(mode="json"))

    async def consume_messages(self) -> None:
        assert self.ws is not None
        async for raw in self.ws:
            data = json.loads(raw)
            t = data.get("type")
            if t == "welcome":
                log.info("worker.welcomed", server_version=data.get("server_version"))
            elif t == "claim_grant":
                grant = ClaimGrantMsg.model_validate(data)
                # Don't await — run the job concurrently with the consume loop
                # so heartbeats / future cancels can still flow.
                asyncio.create_task(self.run_job(grant))
            elif t == "chat_request":
                req = ChatRequestMsg.model_validate(data)
                asyncio.create_task(self.run_chat(req))
            elif t == "workbench_request":
                wreq = WorkbenchRequestMsg.model_validate(data)
                asyncio.create_task(self.run_workbench_job(wreq))
            elif t == "cancel":
                log.info("worker.cancel_received", task=data.get("task_id"))
                # MVP: we don't currently abort an in-flight runner. Log and continue.
            elif t == "ping":
                pass  # the framing-level ping suffices
            elif t == "error":
                log.warning("worker.server_error", code=data.get("code"), message=data.get("message"))

    async def run_once(self) -> None:
        token = self.settings.worker_shared_secret
        url = f"{self.settings.orchestrator_url}?token={token}"
        log.info("worker.connecting", url=self.settings.orchestrator_url)
        async with websockets.connect(url, max_size=64 * 1024 * 1024, ping_interval=20) as ws:
            self.ws = ws
            await self.register()
            hb = asyncio.create_task(self.heartbeat_loop())
            try:
                await self.consume_messages()
            finally:
                # Cancel the heartbeat task on disconnect — but do NOT set
                # self._stop. That flag is for clean shutdown via signals;
                # setting it here would prevent the outer serve() reconnect
                # loop from spinning back up after a routine disconnect.
                hb.cancel()
                try:
                    await hb
                except (asyncio.CancelledError, Exception):
                    pass

    async def serve(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self.run_once()
                backoff = 1.0
            except Exception as exc:
                log.warning("worker.disconnected", error=str(exc), retry_in=backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)


def _install_signal_handlers(worker: Worker) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker._stop.set)
        except NotImplementedError:
            pass  # not supported on Windows


async def _amain() -> None:
    w = Worker()
    _install_signal_handlers(w)
    await w.serve()


if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        sys.exit(0)
