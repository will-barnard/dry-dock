"""Darkroom — image job dispatch, collection, and storage.

The request/response shape here deliberately differs from `generate.py`, and
the reason is the machine rather than the model. `/api/v1/generate` can block
because a local 32B answers in seconds to tens of seconds. An image job on the
Windows box has a cold path of wake + boot + Docker + checkpoint load + render
— 60-170s — and the box is asleep *precisely because* nobody has asked it for
anything recently. So an image job is:

    submit  → row in `image_jobs` (PENDING)
            → background driver: wake if needed (WAKING), dispatch (RUNNING)
            → N `image_result` messages, one per image, collected off a queue
            → PNGs written to the images volume, row goes DONE

Callers either poll the row (the API, the module UI) or await it with a bounded
timeout (`wait_for_job`, used by the API's optional `wait` and the Operator
tool). Nothing blocks an HTTP request for the cold path.

Single-replica assumption, same as chat and generate: the worker's WS
connection and the driver task live in the same process.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import ImageJob, ImageJobSource, ImageJobStatus
from app.orchestrator.protocol import ImageRequestMsg
from app.orchestrator.registry import LiveWorker, registry

log = structlog.get_logger()

# The pool name an imager registers with. Capability is the real test —
# `pool` is kept as a fallback so a worker that predates the capabilities
# field still resolves.
IMAGER_POOL = "imager"
IMAGE_CAPABILITY = "image"

# Sane bounds. ComfyUI will happily accept nonsense and fail slowly.
MIN_DIM, MAX_DIM = 256, 2048
MAX_STEPS = 80


class ImageError(Exception):
    """Raised for failures a route should translate into an HTTP error."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


# ── in-flight collection ───────────────────────────────────────────
#
# A job's images arrive as N separate `image_result` messages. The driver task
# reads them off this queue; the WS handler writes to it. Keyed by job id.


@dataclass
class _Pending:
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    worker_name: str = ""


_pending: dict[uuid.UUID, _Pending] = {}


def is_pending(job_id: uuid.UUID) -> bool:
    return job_id in _pending


# ── worker selection + wake ────────────────────────────────────────


async def _imagers() -> list[LiveWorker]:
    live = await registry.all()
    return [
        w for w in live
        if IMAGE_CAPABILITY in (w.capabilities or []) or w.pool == IMAGER_POOL
    ]


async def pick_imager() -> LiveWorker | None:
    """Prefer a fully idle imager, but accept a busy one — ComfyUI queues
    internally, so a busy worker still answers, just later."""
    workers = await _imagers()
    if not workers:
        return None
    idle = [w for w in workers if not w.current_image_jobs and w.current_task_id is None]
    return (idle or workers)[0]


async def imager_status() -> dict:
    """What the UI and /image/health both need: is anyone home, and if not,
    is there a machine we could wake?"""
    settings = get_settings()
    workers = await _imagers()
    machine_name = settings.image_machine or None
    online = None
    if machine_name and not workers:
        from app.orchestrator.remote_machines import find_machine, machine_status
        machine = find_machine(machine_name)
        if machine is not None:
            status = await machine_status(machine)
            online = bool(status.get("online"))
    return {
        "imagers": [
            {
                "name": w.name,
                "busy": bool(w.current_image_jobs),
                "checkpoints": (w.metadata or {}).get("checkpoints", []),
                "workflows": (w.metadata or {}).get("workflows", []),
            }
            for w in workers
        ],
        "count": len(workers),
        "machine": machine_name,
        # None means "we didn't check" (an imager was already online).
        "machine_online": online,
        "can_wake": bool(machine_name),
    }


async def ensure_imager_awake(job_id: uuid.UUID | None = None) -> LiveWorker:
    """Return a live imager, waking its machine first if necessary.

    The wake route on the dashboard sits behind the operator session cookie, so
    an external caller holding DRYDOCK_API_KEY cannot use it. Rather than
    loosening that route's auth — the boundary there is about external clients,
    not internal orchestration — we call the host agent directly, exactly as
    `remote_machines.wake_machine` does, and then poll the registry.
    """
    worker = await pick_imager()
    if worker is not None:
        return worker

    settings = get_settings()
    machine_name = settings.image_machine
    if not machine_name:
        raise ImageError(
            "No imager worker is online, and IMAGE_MACHINE is not configured "
            "so there is nothing to wake.",
            status=503,
        )

    from app.orchestrator.remote_machines import find_machine, wake_machine

    machine = find_machine(machine_name)
    if machine is None:
        raise ImageError(
            f"IMAGE_MACHINE is set to '{machine_name}', which is not in "
            "REMOTE_MACHINES_JSON.",
            status=503,
        )

    if job_id is not None:
        await _set_status(job_id, ImageJobStatus.WAKING)

    log.info("image.waking_machine", machine=machine.name, job=str(job_id) if job_id else None)
    result = await wake_machine(machine)
    if not result.get("ok"):
        raise ImageError(
            f"Couldn't wake {machine.display_name}: {result.get('error') or result}",
            status=503,
        )

    deadline = time.monotonic() + settings.image_wake_timeout_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(3.0)
        worker = await pick_imager()
        if worker is not None:
            log.info(
                "image.machine_awake",
                machine=machine.name, worker=worker.name,
                waited_s=round(settings.image_wake_timeout_seconds - (deadline - time.monotonic())),
            )
            return worker

    raise ImageError(
        f"{machine.display_name} was woken but no imager worker registered "
        f"within {settings.image_wake_timeout_seconds:.0f}s. Check that Docker "
        "and ComfyUI start on boot on that machine.",
        status=503,
    )


# ── storage ────────────────────────────────────────────────────────


def job_dir(job_id: uuid.UUID) -> Path:
    return Path(get_settings().image_dir) / str(job_id)


def image_path(job_id: uuid.UUID, index: int, thumb: bool = False) -> Path:
    name = f"{index}_thumb.webp" if thumb else f"{index}.png"
    return job_dir(job_id) / name


def _write_image(job_id: uuid.UUID, index: int, data: bytes) -> None:
    """Write the PNG and a small WEBP thumbnail beside it.

    Thumbnails matter: a gallery of twenty full 1024² PNGs is ~30MB of page
    load. If Pillow is somehow unavailable the thumbnail is skipped and the
    UI falls back to the full image rather than the whole write failing.
    """
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{index}.png").write_bytes(data)
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img.thumbnail((512, 512))
        img.save(d / f"{index}_thumb.webp", "WEBP", quality=82)
    except Exception:  # noqa: BLE001 — thumbnails are a nicety, never fatal
        log.warning("image.thumbnail_failed", job=str(job_id), index=index)


# ── job lifecycle ──────────────────────────────────────────────────


async def _set_status(
    job_id: uuid.UUID,
    status: ImageJobStatus,
    *,
    error: str | None = None,
    worker_name: str | None = None,
    result: dict | None = None,
) -> None:
    async with SessionLocal() as session:
        async with session.begin():
            job = await session.get(ImageJob, job_id)
            if job is None:
                return
            job.status = status
            if error is not None:
                job.error = error
            if worker_name is not None:
                job.worker_name = worker_name
            if result is not None:
                job.result = result


def normalize_params(raw: dict) -> dict:
    """Clamp and default everything a caller can set. ComfyUI accepts nonsense
    and fails slowly, so reject it here where the error is cheap."""
    settings = get_settings()

    def _clamp(value, lo, hi, default):
        try:
            v = type(default)(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))

    width = _clamp(raw.get("width", 1024), MIN_DIM, MAX_DIM, 1024)
    height = _clamp(raw.get("height", 1024), MIN_DIM, MAX_DIM, 1024)
    # SDXL and Flux both want multiples of 8; silently rounding beats a
    # confusing worker-side failure.
    width -= width % 8
    height -= height % 8
    return {
        "workflow": (raw.get("workflow") or settings.image_default_workflow),
        "checkpoint": raw.get("checkpoint") or None,
        "width": width,
        "height": height,
        "steps": _clamp(raw.get("steps", 30), 1, MAX_STEPS, 30),
        "cfg": _clamp(raw.get("cfg", 6.0), 0.0, 30.0, 6.0),
        "sampler": raw.get("sampler") or None,
        "scheduler": raw.get("scheduler") or None,
        "seed": raw.get("seed") if raw.get("seed") not in (None, "") else None,
        "batch": _clamp(raw.get("batch", 1), 1, settings.image_max_batch, 1),
    }


async def api_jobs_today() -> int:
    """Images requested through the keyed API since midnight UTC."""
    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with SessionLocal() as session:
        return (await session.execute(
            select(func.count()).select_from(ImageJob).where(
                ImageJob.source == ImageJobSource.API, ImageJob.created_at >= since
            )
        )).scalar_one()


async def submit_job(
    prompt: str,
    *,
    negative_prompt: str | None = None,
    params: dict | None = None,
    source: ImageJobSource = ImageJobSource.MODULE,
    conversation_id: uuid.UUID | None = None,
) -> ImageJob:
    """Create the job row and start its driver task. Returns immediately."""
    if not (prompt or "").strip():
        raise ImageError("Prompt is empty.", status=422)

    settings = get_settings()
    if source is ImageJobSource.API and settings.image_daily_budget:
        if await api_jobs_today() >= settings.image_daily_budget:
            raise ImageError(
                f"Daily image budget of {settings.image_daily_budget} reached.",
                status=429,
            )

    # Pre-flight: fail fast when there is neither an imager nor a machine to
    # wake. That's a configuration/availability problem the caller can act on
    # immediately — making them accept a 202 and poll only to be told "no
    # imager" wastes a round trip and reads like the job failed on its merits.
    # When a wake path DOES exist we return 202 as usual, because waking is
    # legitimately a minutes-long operation.
    if await pick_imager() is None and not settings.image_machine:
        raise ImageError(
            "No imager worker is online, and IMAGE_MACHINE is not configured "
            "so there is nothing to wake.",
            status=503,
        )

    normalized = normalize_params(params or {})
    async with SessionLocal() as session:
        async with session.begin():
            job = ImageJob(
                prompt=prompt.strip(),
                negative_prompt=(negative_prompt or None),
                params=normalized,
                source=source,
                conversation_id=conversation_id,
                status=ImageJobStatus.PENDING,
            )
            session.add(job)
        await session.refresh(job)

    asyncio.create_task(_drive_job(job.id), name=f"image_job_{job.id}")
    log.info("image.submitted", job=str(job.id), source=source.value)
    return job


async def _drive_job(job_id: uuid.UUID) -> None:
    """Background driver: wake, dispatch, collect, persist. Never raises."""
    settings = get_settings()
    pending = _Pending()
    _pending[job_id] = pending
    worker: LiveWorker | None = None
    try:
        async with SessionLocal() as session:
            job = await session.get(ImageJob, job_id)
            if job is None:
                return
            prompt, negative = job.prompt, job.negative_prompt
            params = dict(job.params or {})

        worker = await ensure_imager_awake(job_id)
        pending.worker_name = worker.name
        worker.current_image_jobs.add(job_id)

        seed = params.get("seed")
        if seed is None:
            seed = random.randint(0, 2**32 - 1)

        msg = ImageRequestMsg(
            job_id=job_id,
            workflow=params.get("workflow") or settings.image_default_workflow,
            prompt=prompt,
            negative_prompt=negative,
            checkpoint=params.get("checkpoint"),
            width=params.get("width", 1024),
            height=params.get("height", 1024),
            steps=params.get("steps", 30),
            cfg=params.get("cfg", 6.0),
            sampler=params.get("sampler"),
            scheduler=params.get("scheduler"),
            seed=seed,
            batch=params.get("batch", 1),
        )
        await _set_status(job_id, ImageJobStatus.RUNNING, worker_name=worker.name)
        await worker.send(msg.model_dump(mode="json"))
        log.info("image.dispatched", job=str(job_id), worker=worker.name,
                 workflow=msg.workflow, batch=msg.batch)

        images: list[dict] = []
        checkpoint_used: str | None = None
        expected = msg.batch
        deadline = time.monotonic() + settings.image_job_timeout_seconds

        while len(images) < expected:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ImageError(
                    f"Render timed out after {settings.image_job_timeout_seconds:.0f}s "
                    f"on {worker.name}.",
                    status=504,
                )
            try:
                item = await asyncio.wait_for(pending.queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                raise ImageError(
                    f"Render timed out after {settings.image_job_timeout_seconds:.0f}s "
                    f"on {worker.name}.",
                    status=504,
                ) from None

            if isinstance(item, ImageError):
                raise item
            if not item.get("success"):
                raise ImageError(item.get("error") or "Worker reported an error.")

            # The worker is authoritative about how many images are coming.
            expected = item.get("total") or expected
            index = item.get("index", len(images))
            try:
                data = base64.b64decode(item.get("image_b64") or "", validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ImageError(f"Worker sent undecodable image data: {exc}") from exc
            if not data:
                raise ImageError("Worker sent an empty image.")

            _write_image(job_id, index, data)
            checkpoint_used = item.get("checkpoint") or checkpoint_used
            images.append({
                "index": index,
                "seed": item.get("seed"),
                "width": item.get("width") or params.get("width"),
                "height": item.get("height") or params.get("height"),
                "elapsed_ms": item.get("elapsed_ms", 0),
            })

        images.sort(key=lambda i: i["index"])
        await _set_status(
            job_id,
            ImageJobStatus.DONE,
            result={"images": images, "checkpoint": checkpoint_used},
        )
        log.info("image.done", job=str(job_id), count=len(images),
                 checkpoint=checkpoint_used, worker=worker.name)

    except ImageError as exc:
        log.warning("image.failed", job=str(job_id), error=str(exc))
        await _set_status(job_id, ImageJobStatus.ERROR, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("image.driver_crashed", job=str(job_id))
        await _set_status(job_id, ImageJobStatus.ERROR, error=str(exc))
    finally:
        _pending.pop(job_id, None)
        if worker is not None:
            worker.current_image_jobs.discard(job_id)


# ── inbound from the WS handler ────────────────────────────────────


def resolve_image(
    job_id: uuid.UUID,
    *,
    success: bool,
    index: int,
    total: int,
    image_b64: str,
    seed: int | None,
    checkpoint: str | None,
    width: int,
    height: int,
    elapsed_ms: int,
    error: str | None,
) -> bool:
    """Hand one `image_result` to the driver. Returns False if the job isn't
    one of ours (already timed out, or the orchestrator restarted)."""
    pending = _pending.get(job_id)
    if pending is None:
        return False
    pending.queue.put_nowait({
        "success": success,
        "index": index,
        "total": total,
        "image_b64": image_b64,
        "seed": seed,
        "checkpoint": checkpoint,
        "width": width,
        "height": height,
        "elapsed_ms": elapsed_ms,
        "error": error,
    })
    return True


def fail_image_jobs(job_ids: set[uuid.UUID], worker_name: str) -> None:
    """Reject in-flight image jobs whose worker disconnected. The driver task
    owns the DB write, so we just push the failure onto its queue."""
    for job_id in job_ids:
        pending = _pending.get(job_id)
        if pending is not None:
            pending.queue.put_nowait(
                ImageError(
                    f"Worker {worker_name} disconnected mid-render.", status=503
                )
            )


# ── waiting (optional, bounded) ────────────────────────────────────


async def wait_for_job(job_id: uuid.UUID, timeout: float) -> ImageJob | None:
    """Poll the row until the job leaves a non-terminal state or `timeout`
    expires. Used by the API's optional `wait` and by the Operator tool.
    Returns the job (terminal or not) — callers decide what to do with a job
    that is still running."""
    deadline = time.monotonic() + timeout
    while True:
        async with SessionLocal() as session:
            job = await session.get(ImageJob, job_id)
        if job is None:
            return None
        if job.status in (ImageJobStatus.DONE, ImageJobStatus.ERROR):
            return job
        if time.monotonic() >= deadline:
            return job
        await asyncio.sleep(1.0)


# ── watchdog + retention ───────────────────────────────────────────

IMAGE_WATCHDOG_INTERVAL_SECONDS = 60.0
# Generous: the wake path alone can legitimately sit in WAKING for 3 minutes.
IMAGE_STALE_AFTER = timedelta(minutes=15)


async def _sweep_stale_image_jobs() -> int:
    """Move jobs that no live driver owns out of PENDING/WAKING/RUNNING.

    Without this, an orchestrator restart mid-render leaves a row spinning
    forever — the driver task died with the process but the row never learned.
    """
    cutoff = datetime.now(timezone.utc) - IMAGE_STALE_AFTER
    swept = 0
    async with SessionLocal() as session:
        async with session.begin():
            rows = (await session.execute(
                select(ImageJob).where(
                    ImageJob.status.in_((
                        ImageJobStatus.PENDING,
                        ImageJobStatus.WAKING,
                        ImageJobStatus.RUNNING,
                    )),
                    ImageJob.updated_at < cutoff,
                )
            )).scalars().all()
            for job in rows:
                if is_pending(job.id):
                    continue  # a driver is genuinely still working on it
                job.status = ImageJobStatus.ERROR
                job.error = (
                    "Job was abandoned — the orchestrator most likely restarted "
                    "mid-render. Re-roll to try again."
                )
                swept += 1
    if swept:
        log.warning("image.watchdog_swept", count=swept)
    return swept


async def _sweep_old_images() -> int:
    """Delete rendered files past the retention window. Rows are kept — they're
    small, and the prompt history is worth more than the pixels."""
    days = get_settings().image_retention_days
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    removed = 0
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(ImageJob.id).where(
                ImageJob.created_at < cutoff, ImageJob.status == ImageJobStatus.DONE
            )
        )).scalars().all()
    for job_id in rows:
        d = job_dir(job_id)
        if not d.exists():
            continue
        for f in d.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        try:
            d.rmdir()
            removed += 1
        except OSError:
            pass
    if removed:
        log.info("image.retention_swept", jobs=removed, days=days)
    return removed


async def image_watchdog_loop(stop: asyncio.Event) -> None:
    log.info("image.watchdog_started", interval=IMAGE_WATCHDOG_INTERVAL_SECONDS)
    while not stop.is_set():
        try:
            await _sweep_stale_image_jobs()
            await _sweep_old_images()
        except Exception:
            log.exception("image.watchdog_error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=IMAGE_WATCHDOG_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
    log.info("image.watchdog_stopped")
