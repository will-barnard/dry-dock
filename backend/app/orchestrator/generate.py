"""Synchronous one-shot generation for external API callers.

This is the request/response cousin of Operator chat. Where chat streams
deltas back to a browser over SSE, `generate` is a blocking call: an external
app (e.g. seedbook) POSTs a prompt, we dispatch a single non-streaming
inference to a live worker, wait for the worker's reply, and hand the finished
text straight back in the HTTP response.

Mechanics reuse the existing Workbench message pair (`workbench_request` /
`workbench_result`) with a dedicated `kind="generate"`. The worker's
`run_workbench_job` is kind-agnostic — it just runs `provider.chat(model,
messages)` and returns the content — so **no worker change is required**. The
only new machinery here is correlation: we key an asyncio.Future by the job id
and resolve it when the matching `workbench_result` lands on the WS handler.

Single-replica assumption (same as chat): the worker's WS connection and the
HTTP request that is awaiting the Future live in the same process. If we ever
run multiple orchestrator replicas we'll need a shared result bus; until then
this is correct and simple.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

import structlog

from app.orchestrator.protocol import WorkbenchRequestMsg
from app.orchestrator.registry import LiveWorker, registry

log = structlog.get_logger()

# The Workbench `kind` we tag generate jobs with. Kept distinct from the
# resume/workbench kinds so the WS result dispatcher can route it here instead
# of into the Workbench DB handlers.
GENERATE_KIND = "generate"

# Pools tried in order when a caller doesn't pin one. `researcher` is the
# natural home for free-form text generation; the rest are fallbacks so a
# single-pool fleet still answers.
DEFAULT_POOLS: tuple[str, ...] = ("researcher", "planner", "docs", "coder", "reviewer")


@dataclass
class _Pending:
    future: asyncio.Future[str]
    worker_name: str


# job_id -> pending future. Resolved by resolve_generate() from the WS handler,
# or rejected on worker disconnect / timeout.
_pending: dict[uuid.UUID, _Pending] = {}


class GenerateError(Exception):
    """Raised for any failure the route should translate into an HTTP error.

    `status` is the HTTP status code the route should return.
    """

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


async def _pick_worker(pool: str | None) -> LiveWorker:
    """Choose a live worker. If `pool` is given, only that pool is considered;
    otherwise walk DEFAULT_POOLS. Prefer a fully idle worker but fall back to a
    busy one (Ollama serializes inference, so a busy worker still answers)."""
    pools = (pool,) if pool else DEFAULT_POOLS
    for p in pools:
        workers = await registry.by_pool(p)
        if not workers:
            continue
        idle = [w for w in workers if w.current_task_id is None]
        return (idle or workers)[0]
    where = f"pool '{pool}'" if pool else "any pool"
    raise GenerateError(f"No worker is online in {where}.", status=503)


async def run_generate(
    messages: list[dict[str, str]],
    *,
    pool: str | None = None,
    model: str | None = None,
    timeout: float = 120.0,
) -> dict:
    """Dispatch one blocking inference to a worker and return its output.

    Returns a dict: {content, model, worker, pool, elapsed_ms}. Raises
    GenerateError on any failure (no worker, send failure, worker error,
    timeout) with an appropriate HTTP status attached.
    """
    worker = await _pick_worker(pool)
    job_id = uuid.uuid4()
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    _pending[job_id] = _Pending(future=future, worker_name=worker.name)
    worker.current_workbench_jobs.add(job_id)

    msg = WorkbenchRequestMsg(
        job_id=job_id, kind=GENERATE_KIND, model=model, messages=messages
    )
    started = time.monotonic()
    try:
        await worker.send(msg.model_dump(mode="json"))
    except Exception as exc:  # send failed — socket likely dead
        _pending.pop(job_id, None)
        worker.current_workbench_jobs.discard(job_id)
        raise GenerateError(f"Failed to reach worker {worker.name}: {exc}") from exc

    log.info("generate.dispatched", job=str(job_id), worker=worker.name, pool=worker.pool)
    try:
        content = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise GenerateError(
            f"Generation timed out after {timeout:.0f}s on worker {worker.name}.",
            status=504,
        ) from exc
    finally:
        _pending.pop(job_id, None)
        worker.current_workbench_jobs.discard(job_id)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    log.info("generate.done", job=str(job_id), worker=worker.name, elapsed_ms=elapsed_ms)
    return {
        "content": content,
        "model": model,
        "worker": worker.name,
        "pool": worker.pool,
        "elapsed_ms": elapsed_ms,
    }


def resolve_generate(
    job_id: uuid.UUID, success: bool, content: str, error: str | None
) -> bool:
    """Resolve a pending generate future from the WS result handler.

    Returns True if this job_id belonged to a generate call (so the caller
    knows it handled the message and shouldn't fall through to the Workbench
    handlers). No-op returning True if the future is already resolved
    (timed out then a late result arrived)."""
    pending = _pending.get(job_id)
    if pending is None:
        return False
    fut = pending.future
    if not fut.done():
        if success:
            fut.set_result(content or "")
        else:
            fut.set_exception(GenerateError(error or "Worker reported an error."))
    return True


def fail_generate_jobs(job_ids: set[uuid.UUID], worker_name: str) -> None:
    """Reject any in-flight generate futures whose worker disconnected."""
    for job_id in job_ids:
        pending = _pending.get(job_id)
        if pending and not pending.future.done():
            pending.future.set_exception(
                GenerateError(
                    f"Worker {worker_name} disconnected before finishing generation.",
                    status=503,
                )
            )
