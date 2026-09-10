"""Darkroom module — HTMX views for image generation.

The UI half of the image pipeline. Everything stateful lives in
`orchestrator/image_jobs.py`; this module is routes and rendering only, the
same split Operator and Workbench use.

Job cards poll themselves via HTMX (`hx-trigger="load delay:2s"`) rather than
riding SSE. Deliberate for now: a render is a handful of state changes over
tens of seconds, not a token stream, and polling one row costs less than
standing up another event topic. `streams.py` is the upgrade path if the
gallery ever wants live updates across tabs.
"""
from __future__ import annotations

import random
import uuid
from pathlib import Path
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import get_settings
from app.db import get_session
from app.models import ImageJob, ImageJobSource, User
from app.orchestrator.image_jobs import (
    ImageError,
    image_path,
    imager_status,
    submit_job,
)

log = structlog.get_logger()
router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

GALLERY_LIMIT = 60

# Size presets that are friendly to SDXL's training resolutions. Free-form
# width/height still work through the API; the UI offers the ones that behave.
# Used only when the imager didn't advertise its own lists (probe failed, or
# an older worker). The authoritative list always comes from ComfyUI itself.
FALLBACK_SAMPLERS = [
    "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_3m_sde", "dpmpp_sde",
    "euler", "euler_ancestral", "ddim", "uni_pc",
]
FALLBACK_SCHEDULERS = ["karras", "normal", "exponential", "sgm_uniform", "simple", "beta"]

SIZE_PRESETS: list[tuple[str, int, int]] = [
    ("Square 1024×1024", 1024, 1024),
    ("Portrait 832×1216", 832, 1216),
    ("Landscape 1216×832", 1216, 832),
    ("Wide 1344×768", 1344, 768),
    ("Square (fast) 768×768", 768, 768),
]


async def _gallery(session: AsyncSession) -> list[ImageJob]:
    return list((await session.execute(
        select(ImageJob).order_by(ImageJob.created_at.desc()).limit(GALLERY_LIMIT)
    )).scalars().all())


@router.get("/darkroom", response_class=HTMLResponse)
async def darkroom(
    request: Request,
    error: str | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    status = await imager_status()
    # Checkpoints come from whatever the live imager advertised at register
    # time, so the dropdown can never offer something the box doesn't have.
    checkpoints: list[str] = []
    workflows: list[str] = []
    samplers: list[str] = []
    schedulers: list[str] = []
    for w in status["imagers"]:
        for key, into in (
            ("checkpoints", checkpoints), ("workflows", workflows),
            ("samplers", samplers), ("schedulers", schedulers),
        ):
            for item in w.get(key) or []:
                if item not in into:
                    into.append(item)
    return templates.TemplateResponse(
        request,
        "darkroom.html",
        {
            "user": user,
            "jobs": await _gallery(session),
            "imager": status,
            "checkpoints": checkpoints,
            "workflows": workflows or [get_settings().image_default_workflow],
            # Fall back to the names every ComfyUI build ships, so the controls
            # still work if the probe failed at register time.
            "samplers": samplers or FALLBACK_SAMPLERS,
            "schedulers": schedulers or FALLBACK_SCHEDULERS,
            "size_presets": SIZE_PRESETS,
            "max_batch": get_settings().image_max_batch,
            "error": error,
        },
    )


@router.post("/darkroom/generate")
async def generate_image(
    prompt: str = Form(...),
    negative_prompt: str = Form(""),
    workflow: str = Form(""),
    checkpoint: str = Form(""),
    size: str = Form("1024x1024"),
    steps: int = Form(30),
    cfg: float = Form(6.0),
    sampler: str = Form(""),
    scheduler: str = Form(""),
    seed: str = Form(""),
    batch: int = Form(1),
) -> RedirectResponse:
    try:
        width, height = (int(x) for x in size.lower().split("x", 1))
    except ValueError:
        width, height = 1024, 1024
    try:
        await submit_job(
            prompt,
            negative_prompt=negative_prompt or None,
            params={
                "workflow": workflow or None,
                "checkpoint": checkpoint or None,
                "width": width,
                "height": height,
                "steps": steps,
                "cfg": cfg,
                # Blank means "whatever the workflow template specifies" —
                # normalize_params turns "" into None, which render_graph
                # skips, leaving the template's own value in place.
                "sampler": sampler or None,
                "scheduler": scheduler or None,
                "seed": int(seed) if seed.strip().isdigit() else None,
                "batch": batch,
            },
            source=ImageJobSource.MODULE,
        )
    except ImageError as exc:
        # Bounce back to the module with the reason rather than an error page —
        # the most likely cause is "the GPU box isn't set up yet", which the
        # user fixes on this screen.
        return RedirectResponse(f"/darkroom?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/darkroom", status_code=303)


@router.post("/darkroom/jobs/{job_id}/reroll")
async def reroll(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Same prompt, same parameters, new seed. Cheap because `params` was
    stored as a blob rather than flattened into columns."""
    job = await session.get(ImageJob, job_id)
    if job is None:
        raise HTTPException(404, "No such job.")
    params = dict(job.params or {})
    params["seed"] = random.randint(0, 2**32 - 1)
    await submit_job(
        job.prompt,
        negative_prompt=job.negative_prompt,
        params=params,
        source=ImageJobSource.MODULE,
    )
    return RedirectResponse("/darkroom", status_code=303)


@router.get("/darkroom/jobs/{job_id}/card", response_class=HTMLResponse)
async def job_card(
    request: Request,
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """One job card. Re-polls itself until the job reaches a terminal state,
    at which point the template stops emitting the hx-trigger."""
    job = await session.get(ImageJob, job_id)
    if job is None:
        raise HTTPException(404, "No such job.")
    return templates.TemplateResponse(
        request, "_darkroom_job.html", {"job": job}
    )


@router.post("/darkroom/wake")
async def wake_imager() -> RedirectResponse:
    """Wake the imager's machine from the module UI.

    The API path can't use this route (it's cookie-gated) — it calls the host
    agent directly through `ensure_imager_awake`. Here the cookie exists, so
    reuse the ordinary machine control.
    """
    from app.orchestrator.remote_machines import find_machine, wake_machine

    name = get_settings().image_machine
    machine = find_machine(name) if name else None
    if machine is None:
        raise HTTPException(400, "IMAGE_MACHINE is not configured or not in REMOTE_MACHINES_JSON.")
    await wake_machine(machine)
    return RedirectResponse("/darkroom", status_code=303)


@router.post("/darkroom/refine", response_class=HTMLResponse)
async def refine_prompt(request: Request, prompt: str = Form(...)) -> HTMLResponse:
    """Turn a sentence into a diffusion prompt using the text fleet.

    Free leverage: dry-dock already has models sitting there. Dispatched
    unpinned so it lands on the always-on Mac mini and survives a sleeping
    MacBook — the same availability reasoning as `/api/v1/generate`.
    """
    from app.orchestrator.generate import GenerateError, run_generate

    system = (
        "You rewrite plain descriptions into prompts for a Stable Diffusion "
        "XL image model. Reply with EXACTLY two lines and nothing else:\n"
        "PROMPT: <dense comma-separated visual description — subject, setting, "
        "lighting, lens/composition, art style, quality tags>\n"
        "NEGATIVE: <comma-separated things to avoid for this image>\n"
        "Keep the user's actual subject. Do not invent a different scene. Do "
        "not explain yourself or add any other lines."
    )
    try:
        result = await run_generate(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompt}],
            timeout=60.0,
        )
    except GenerateError as exc:
        return HTMLResponse(
            f'<div class="text-xs text-rose-600">Couldn\'t refine: {exc}</div>',
            status_code=200,
        )

    refined, negative = prompt, ""
    for line in (result.get("content") or "").splitlines():
        line = line.strip()
        if line.upper().startswith("PROMPT:"):
            refined = line.split(":", 1)[1].strip()
        elif line.upper().startswith("NEGATIVE:"):
            negative = line.split(":", 1)[1].strip()

    return templates.TemplateResponse(
        request,
        "_darkroom_refined.html",
        {"prompt": refined, "negative": negative, "worker": result.get("worker")},
    )


@router.get("/darkroom/images/{job_id}/{filename}")
async def darkroom_image(job_id: uuid.UUID, filename: str) -> Response:
    """Cookie-gated twin of the API's file route — the whole router is mounted
    behind `get_current_user`, so the volume is never publicly listable."""
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
