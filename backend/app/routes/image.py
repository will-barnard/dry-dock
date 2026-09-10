"""Public image API — Darkroom for external apps.

`POST /api/v1/image` is the sibling of `/api/v1/generate`, with one deliberate
difference: **it does not block.** Generate can block because a local 32B
answers in seconds. An image job on a sleeping Windows box is wake + boot +
Docker + checkpoint load + render, 60-170s cold — and the box is asleep exactly
because nobody has asked it for anything. A blocking endpoint would therefore
fail in the most common case, so this one is submit → poll:

    POST /api/v1/image              → 202 {job_id, status, poll_url}
    GET  /api/v1/image/{id}         → {status, images: [...], error}
    GET  /api/v1/image/{id}/file/0.png
    GET  /api/v1/image/health

`wait` is an optional convenience for the warm case: block up to N seconds and
return the finished job if it lands in time. It degrades to 202, never to 504.

See IMAGE-API.md in the repo root for the full contract and drop-in clients.
"""
from __future__ import annotations

import base64
import binascii
import uuid

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field

from app.api_auth import require_api_key
from app.config import get_settings
from app.db import SessionLocal
from app.models import ImageJob, ImageJobSource, ImageJobStatus
from app.orchestrator.image_jobs import (
    ImageError,
    image_path,
    imager_status,
    submit_job,
    wait_for_job,
)

router = APIRouter(prefix="/api/v1", tags=["image"])

# Ceiling on the optional `wait`. Long enough to cover a warm render, short
# enough that no sane HTTP client times out underneath us.
MAX_WAIT_SECONDS = 60.0


class ImageRequest(BaseModel):
    prompt: str = Field(description="What to draw.")
    negative_prompt: str | None = Field(
        default=None, description="What to avoid. Optional."
    )
    workflow: str | None = Field(
        default=None,
        description="Named workflow template on the worker, e.g. 'sdxl_txt2img'.",
    )
    checkpoint: str | None = Field(
        default=None, description="Checkpoint filename. Omit for the worker default."
    )
    width: int = 1024
    height: int = 1024
    steps: int = 30
    cfg: float = 6.0
    sampler: str | None = None
    scheduler: str | None = None
    seed: int | None = Field(
        default=None, description="Omit for a random seed (reported back in the result)."
    )
    batch: int = Field(default=1, description="Images to render. Capped server-side.")
    init_image_b64: str | None = Field(
        default=None,
        description=(
            "Base64 source image for img2img. Any common format; it's decoded, "
            "flattened to RGB and resized to about a megapixel server-side. "
            "Supplying one switches the job to the img2img workflow, and the "
            "output takes its dimensions from this image rather than "
            "width/height."
        ),
    )
    denoise: float | None = Field(
        default=None,
        description=(
            "Only used with init_image_b64. How much of the source to discard: "
            "~0.3 retouches, ~0.6 restyles, ~0.85 keeps just the composition. "
            "Defaults to 0.6."
        ),
        gt=0, le=1.0,
    )
    wait: float | None = Field(
        default=None,
        description=(
            "Block up to this many seconds for the result. Capped at 60. If the "
            "job isn't finished by then you still get 202 and a poll URL — never "
            "a timeout error."
        ),
        gt=0,
    )


class ImageInfo(BaseModel):
    index: int
    url: str
    thumb_url: str
    seed: int | None = None
    width: int | None = None
    height: int | None = None


class ImageJobResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    poll_url: str
    prompt: str
    images: list[ImageInfo] = []
    checkpoint: str | None = None
    worker: str | None = None
    error: str | None = None


def _serialize(job: ImageJob) -> ImageJobResponse:
    base = get_settings().drydock_base_url.rstrip("/")
    images: list[ImageInfo] = []
    for item in ((job.result or {}).get("images") or []):
        idx = item.get("index", 0)
        images.append(ImageInfo(
            index=idx,
            url=f"{base}/api/v1/image/{job.id}/file/{idx}.png",
            thumb_url=f"{base}/api/v1/image/{job.id}/file/{idx}_thumb.webp",
            seed=item.get("seed"),
            width=item.get("width"),
            height=item.get("height"),
        ))
    return ImageJobResponse(
        job_id=job.id,
        status=job.status.value,
        poll_url=f"{base}/api/v1/image/{job.id}",
        prompt=job.prompt,
        images=images,
        checkpoint=(job.result or {}).get("checkpoint"),
        worker=job.worker_name,
        error=job.error,
    )


@router.get("/image/health")
async def image_health(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> dict:
    """Auth + readiness probe.

    Unlike `/generate/health` — which returns ok with zero workers online and
    is therefore useless as a readiness gate — this one tells you whether an
    imager is actually there, and whether there's a machine worth waking.
    """
    require_api_key(x_api_key, authorization, what="Image API")
    status = await imager_status()
    return {
        "status": "ok" if status["count"] else "no_imager",
        "imagers": status["count"],
        "workers": status["imagers"],
        "machine": status["machine"],
        "machine_online": status["machine_online"],
        "can_wake": status["can_wake"],
    }


@router.post("/image", response_model=ImageJobResponse, status_code=202)
async def create_image(
    body: ImageRequest,
    response: Response,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> ImageJobResponse:
    require_api_key(x_api_key, authorization, what="Image API")

    raw: bytes | None = None
    if body.init_image_b64:
        try:
            raw = base64.b64decode(body.init_image_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(422, f"init_image_b64 isn't valid base64: {exc}") from exc

    try:
        job = await submit_job(
            body.prompt,
            negative_prompt=body.negative_prompt,
            params={
                "workflow": body.workflow,
                "checkpoint": body.checkpoint,
                "width": body.width,
                "height": body.height,
                "steps": body.steps,
                "cfg": body.cfg,
                "sampler": body.sampler,
                "scheduler": body.scheduler,
                "seed": body.seed,
                "batch": body.batch,
                "denoise": body.denoise if body.denoise is not None else 0.6,
            },
            source=ImageJobSource.API,
            init_image=raw,
        )
    except ImageError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    if body.wait:
        finished = await wait_for_job(job.id, min(body.wait, MAX_WAIT_SECONDS))
        if finished is not None:
            job = finished
            if job.status is ImageJobStatus.DONE:
                response.status_code = 200
            elif job.status is ImageJobStatus.ERROR:
                # The submission succeeded; the render didn't. 200 with an
                # error field beats a 5xx that implies the API is broken.
                response.status_code = 200

    return _serialize(job)


@router.get("/image/{job_id}", response_model=ImageJobResponse)
async def get_image_job(
    job_id: uuid.UUID,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> ImageJobResponse:
    require_api_key(x_api_key, authorization, what="Image API")
    async with SessionLocal() as session:
        job = await session.get(ImageJob, job_id)
    if job is None:
        raise HTTPException(404, "No such image job.")
    return _serialize(job)


@router.get("/image/{job_id}/file/{filename}")
async def get_image_file(
    job_id: uuid.UUID,
    filename: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> Response:
    """Serve the bytes through the keyed route rather than a static mount, so
    the images volume never becomes a public directory."""
    require_api_key(x_api_key, authorization, what="Image API")

    thumb = filename.endswith("_thumb.webp")
    stem = filename.split("_")[0] if thumb else filename.removesuffix(".png")
    try:
        index = int(stem)
    except ValueError:
        raise HTTPException(404, "Not found.") from None

    path = image_path(job_id, index, thumb=thumb)
    if not path.exists():
        raise HTTPException(404, "Not found.")
    return Response(
        content=path.read_bytes(),
        media_type="image/webp" if thumb else "image/png",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )
