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
import base64
import json
import logging
import platform
import signal
import sys
import time
import uuid
from typing import Any

import structlog
import websockets

from app.comfy_client import ComfyClient, ComfyError, load_workflows, render_graph
from app.config import get_settings
from app.ollama_client import get_provider
from app.protocol import (
    ArtifactMsg,
    ChatChunkMsg,
    ChatDoneMsg,
    ChatErrorMsg,
    ChatRequestMsg,
    ChatToolCallMsg,
    ChatToolResultMsg,
    ClaimGrantMsg,
    ClaimRequestMsg,
    HeartbeatMsg,
    ImageRequestMsg,
    ImageResultMsg,
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


def _chat_options() -> dict[str, Any]:
    """Inference options for the conversational paths — Operator chat, the
    tool loop, Workbench, and the generate API.

    These had the same defect as the task runners: no options meant Ollama's
    4096-token default, which truncates from the front and quietly eats the
    system prompt and the older half of a conversation. Temperature is higher
    than the runners' on purpose — chat and research want some variety;
    structured code edits do not.
    """
    s = get_settings()
    return {
        "num_ctx": s.max_context,
        "temperature": s.chat_temperature,
        "top_p": s.top_p,
        "num_predict": s.num_predict,
    }


class Worker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.ws: websockets.WebSocketClientProtocol | None = None
        self.current_task: uuid.UUID | None = None
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()
        # Futures awaiting a ChatToolResultMsg, keyed by tool_call_id. The
        # run_chat tool loop creates one when it emits a tool call; the
        # consume loop resolves it when the orchestrator's result arrives.
        self._pending_tool_calls: dict[str, asyncio.Future] = {}
        # Darkroom state. Populated at register() time on an imager and left
        # empty everywhere else, so a text worker carries no ComfyUI baggage.
        self.comfy = ComfyClient()
        self.workflows: dict[str, dict] = {}
        self.checkpoints: list[str] = []

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
        metadata: dict[str, Any] = {"platform": platform.platform()}

        if self.settings.is_imager:
            # An imager never talks to Ollama — asking it for a model list on a
            # box where it isn't running just produces a scary log line. Its
            # "installed models" are ComfyUI checkpoints, advertised so the
            # Darkroom UI can only ever offer what this machine really has.
            self.workflows = load_workflows()
            await self.wait_for_comfy()
            self.checkpoints = await self.comfy.list_checkpoints()
            installed = list(self.checkpoints)
            metadata["checkpoints"] = self.checkpoints
            metadata["workflows"] = sorted(self.workflows)
            metadata["comfyui"] = self.settings.comfyui_base_url
            if not self.checkpoints:
                log.warning(
                    "comfy.no_checkpoints",
                    url=self.settings.comfyui_base_url,
                    hint="ComfyUI unreachable or has no checkpoints installed",
                )
        else:
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
            capabilities=self.settings.capabilities,
            metadata=metadata,
        )
        await self.send(reg.model_dump(mode="json"))
        log.info(
            "worker.registered",
            pool=self.settings.worker_pool,
            capabilities=self.settings.capabilities,
            models=installed,
        )

        # Imagers have no runner for their pool and no task kind maps to them,
        # so asking for task work would be noise. Image jobs are pushed, not
        # claimed.
        if not self.settings.is_imager:
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

    # Bound the tool loop so a confused model can't ping-pong forever. Set
    # generously: a thorough research turn is a search + several fetches +
    # maybe a follow-up search, which adds up fast. The orchestrator's
    # per-search budget is the real cost ceiling.
    _MAX_TOOL_ITERATIONS = 12

    async def run_chat(self, req: ChatRequestMsg) -> None:
        """Answer one Operator conversation turn.

        Two paths:
          - No tools → stream chat_chunk deltas, finish with chat_done. (Phase 1.)
          - Tools supplied → run a tool-calling loop: call Ollama with the
            tools, and whenever the model emits tool_calls, ship each one to
            the orchestrator, await the result, append it, and loop. The final
            tool-free message is the answer. (Phase 2.)
        Runs concurrently with the consume loop, same as run_job.
        """
        if req.tools:
            await self._run_chat_with_tools(req)
            return

        provider = get_provider()
        model = req.model or self.settings.default_model
        log.info("worker.chat_started", conversation=str(req.conversation_id), model=model)
        chunks: list[str] = []
        tokens_in = 0
        tokens_out = 0
        try:
            async for ev in provider.chat_stream(
                model, req.messages, options=_chat_options()
            ):
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

    async def _call_tool(
        self, req: ChatRequestMsg, name: str, arguments: dict[str, Any]
    ) -> str:
        """Ship one tool call to the orchestrator and await its result. The
        consume loop resolves the future when ChatToolResultMsg arrives."""
        tool_call_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_tool_calls[tool_call_id] = fut
        await self.send(ChatToolCallMsg(
            conversation_id=req.conversation_id,
            assistant_message_id=req.assistant_message_id,
            tool_call_id=tool_call_id,
            name=name,
            arguments=arguments,
        ).model_dump(mode="json"))
        try:
            # Generous ceiling — fetch + extract of a slow page can take a bit.
            result: ChatToolResultMsg = await asyncio.wait_for(fut, timeout=60.0)
        except asyncio.TimeoutError:
            return "Tool call timed out before the orchestrator responded."
        finally:
            self._pending_tool_calls.pop(tool_call_id, None)
        if not result.success:
            return f"Tool error: {result.error or 'unknown error'}"
        return result.content

    async def _run_chat_with_tools(self, req: ChatRequestMsg) -> None:
        """Tool-calling loop. Non-streaming per iteration (Ollama returns
        tool_calls in the message); the final tool-free answer is sent as a
        single chunk + done so the UI updates in one shot."""
        provider = get_provider()
        model = req.model or self.settings.default_model
        log.info("worker.chat_tools_started",
                 conversation=str(req.conversation_id), model=model,
                 tools=[t.get("function", {}).get("name") for t in (req.tools or [])])
        messages = list(req.messages)
        tokens_in = 0
        tokens_out = 0
        try:
            for iteration in range(self._MAX_TOOL_ITERATIONS):
                result = await provider.chat(
                    model, messages, tools=req.tools, options=_chat_options()
                )
                msg = result.get("message") or {}
                tokens_in += result.get("prompt_eval_count", 0) or 0
                tokens_out += result.get("eval_count", 0) or 0
                tool_calls = msg.get("tool_calls") or []

                if not tool_calls:
                    # Final answer.
                    content = msg.get("content") or ""
                    await self.send(ChatChunkMsg(
                        conversation_id=req.conversation_id,
                        assistant_message_id=req.assistant_message_id,
                        delta=content,
                    ).model_dump(mode="json"))
                    await self.send(ChatDoneMsg(
                        conversation_id=req.conversation_id,
                        assistant_message_id=req.assistant_message_id,
                        content=content, tokens_in=tokens_in, tokens_out=tokens_out,
                    ).model_dump(mode="json"))
                    log.info("worker.chat_tools_done",
                             conversation=str(req.conversation_id), iterations=iteration + 1)
                    return

                # Record the assistant's tool-call message in history, then run
                # each tool and append a tool-role message with the result.
                messages.append({
                    "role": "assistant", "content": msg.get("content") or "",
                    "tool_calls": tool_calls,
                })
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or ""
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    tool_output = await self._call_tool(req, name, args)
                    messages.append({
                        "role": "tool", "tool_name": name, "content": tool_output,
                    })

            # Exhausted the iteration budget — make one final tool-free call so
            # the model produces a real answer instead of stalling.
            final = await provider.chat(model, messages, options=_chat_options())
            content = (final.get("message") or {}).get("content") or (
                "I wasn't able to finish researching that within the tool-call "
                "budget. Here's what I found so far."
            )
            await self.send(ChatChunkMsg(
                conversation_id=req.conversation_id,
                assistant_message_id=req.assistant_message_id,
                delta=content,
            ).model_dump(mode="json"))
            await self.send(ChatDoneMsg(
                conversation_id=req.conversation_id,
                assistant_message_id=req.assistant_message_id,
                content=content, tokens_in=tokens_in, tokens_out=tokens_out,
            ).model_dump(mode="json"))
        except Exception as exc:
            log.exception("worker.chat_tools_failed")
            await self.send(ChatErrorMsg(
                conversation_id=req.conversation_id,
                assistant_message_id=req.assistant_message_id,
                error=str(exc),
            ).model_dump(mode="json"))

    async def run_workbench_job(self, req: WorkbenchRequestMsg) -> None:
        """Run one non-streaming Workbench inference (resume import, tailoring,
        bullet improvement). Unlike chat, there's no streaming — the
        orchestrator needs the whole (usually JSON) response to act on it, so
        we just call `provider.chat` once and return the full content."""
        provider = get_provider()
        model = req.model or self.settings.default_model
        log.info("worker.workbench_started", job=str(req.job_id), kind=req.kind, model=model)
        try:
            result = await provider.chat(
                model, req.messages, options=_chat_options()
            )
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

    # ── Darkroom ───────────────────────────────────────────────────

    async def wait_for_comfy(self) -> None:
        """Block until ComfyUI answers, or the startup budget runs out.

        Boot ordering, not paranoia: this container restarts with Docker, and
        on a machine that reboots daily that happens minutes before ComfyUI has
        finished starting. Registering during that window advertises zero
        checkpoints, which leaves the Darkroom dropdown empty until someone
        notices. Waiting costs nothing on a machine where ComfyUI is already
        up - the first probe succeeds.

        If the budget expires we register anyway rather than exiting: a visible
        imager reporting a legible error beats a machine that looks absent.
        """
        deadline = time.monotonic() + self.settings.comfyui_startup_wait_seconds
        announced = False
        while time.monotonic() < deadline:
            if await self.comfy.health() is not None:
                if announced:
                    log.info("comfy.ready", url=self.settings.comfyui_base_url)
                return
            if not announced:
                log.info(
                    "comfy.waiting",
                    url=self.settings.comfyui_base_url,
                    budget_s=self.settings.comfyui_startup_wait_seconds,
                )
                announced = True
            await asyncio.sleep(5.0)
        log.warning(
            "comfy.startup_wait_expired",
            url=self.settings.comfyui_base_url,
            hint="registering without checkpoints; ComfyUI may not be running",
        )

    async def refresh_checkpoints(self) -> None:
        """Re-probe ComfyUI for checkpoints. Called before a render when the
        list is empty, so a worker that registered ahead of ComfyUI recovers on
        its own instead of needing a restart."""
        found = await self.comfy.list_checkpoints()
        if found:
            self.checkpoints = found
            log.info("comfy.checkpoints_refreshed", count=len(found))


    def _resolve_checkpoint(self, requested: str | None, template: dict) -> str | None:
        """Pick the checkpoint to actually load.

        Order: what the caller asked for → this worker's configured default →
        whatever the template carries. If the choice isn't installed we fall
        back to the first one that is, rather than letting ComfyUI fail on an
        exact-string miss — the same class of silent filter that bit role-model
        pins on the text fleet.
        """
        candidate = (
            requested
            or self.settings.comfyui_default_checkpoint
            or (template.get("graph", {}).get("4", {}).get("inputs", {}).get("ckpt_name"))
        )
        if not self.checkpoints:
            return candidate  # can't validate; let ComfyUI have the last word
        if candidate in self.checkpoints:
            return candidate
        fallback = self.checkpoints[0]
        log.warning(
            "comfy.checkpoint_not_installed",
            requested=candidate, using=fallback, available=self.checkpoints,
        )
        return fallback

    async def run_image_job(self, req: ImageRequestMsg) -> None:
        """Render one image job on ComfyUI and stream the results back.

        One ImageResultMsg per image: a 1024² PNG is ~1.4-2.7MB base64, and a
        batch of four in a single frame would block this socket for everything
        else on the worker.
        """
        started = time.monotonic()
        log.info("worker.image_started", job=str(req.job_id), workflow=req.workflow,
                 batch=req.batch, steps=req.steps)
        # Self-heal the boot-order case: we registered before ComfyUI was up.
        if not self.checkpoints:
            await self.refresh_checkpoints()
        try:
            if req.graph:
                # Raw-graph escape hatch: lets a new workflow be trialled from
                # the orchestrator without rebuilding this container.
                graph = req.graph
                checkpoint = req.checkpoint
            else:
                template = self.workflows.get(req.workflow)
                if template is None:
                    raise ComfyError(
                        f"unknown workflow '{req.workflow}'. This worker has: "
                        f"{sorted(self.workflows) or 'none'}"
                    )
                checkpoint = self._resolve_checkpoint(req.checkpoint, template)
                graph = render_graph(template, {
                    "checkpoint": checkpoint,
                    "prompt": req.prompt,
                    "negative_prompt": req.negative_prompt or "",
                    "width": req.width,
                    "height": req.height,
                    "batch": req.batch,
                    "seed": req.seed,
                    "steps": req.steps,
                    "cfg": req.cfg,
                    "sampler": req.sampler,
                    "scheduler": req.scheduler,
                })

            prompt_id = await self.comfy.submit(graph)
            images = await self.comfy.wait_for_images(
                prompt_id, timeout=self.settings.comfyui_timeout_seconds
            )
            elapsed = int((time.monotonic() - started) * 1000)

            for i, data in enumerate(images):
                await self.send(ImageResultMsg(
                    job_id=req.job_id,
                    index=i,
                    total=len(images),
                    success=True,
                    image_b64=base64.b64encode(data).decode("ascii"),
                    # A batch shares one seed and varies by batch index, so
                    # every image in the batch reports the same seed.
                    seed=req.seed,
                    checkpoint=checkpoint,
                    width=req.width,
                    height=req.height,
                    elapsed_ms=elapsed,
                ).model_dump(mode="json"))
            log.info("worker.image_done", job=str(req.job_id), count=len(images),
                     elapsed_ms=elapsed, checkpoint=checkpoint)

        except Exception as exc:  # noqa: BLE001
            log.exception("worker.image_failed", job=str(req.job_id))
            await self.send(ImageResultMsg(
                job_id=req.job_id, index=0, total=1, success=False,
                error=str(exc),
                elapsed_ms=int((time.monotonic() - started) * 1000),
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
            elif t == "chat_tool_result":
                res = ChatToolResultMsg.model_validate(data)
                fut = self._pending_tool_calls.get(res.tool_call_id)
                if fut and not fut.done():
                    fut.set_result(res)
            elif t == "workbench_request":
                wreq = WorkbenchRequestMsg.model_validate(data)
                asyncio.create_task(self.run_workbench_job(wreq))
            elif t == "image_request":
                ireq = ImageRequestMsg.model_validate(data)
                asyncio.create_task(self.run_image_job(ireq))
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
