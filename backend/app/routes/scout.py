"""Scout module routes — site profiles + extraction recipes (Phase A).

CRUD over site profiles and their recipes, plus a Test-fetch endpoint that
runs a URL through the live fetch path so you can validate a recipe before
trusting it.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_session
from app.models import (
    ExtractionRecipe,
    ExtractionStrategy,
    SiteLearningJob,
    SiteProfile,
    SiteProfileStatus,
    User,
)
from app.orchestrator import scout, web_fetch

router = APIRouter(tags=["scout"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/scout", response_class=HTMLResponse, response_model=None)
async def scout_home(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    profiles = list((await session.execute(
        select(SiteProfile).order_by(SiteProfile.domain)
    )).scalars().all())
    # Touch recipes (selectin-loaded) so the template can read counts.
    for p in profiles:
        _ = p.recipes
    return templates.TemplateResponse(
        request, "scout.html", {"user": user, "profiles": profiles},
    )


@router.post("/scout/profiles", response_class=HTMLResponse, response_model=None)
async def add_profile(
    domain: str = Form(...),
    display_name: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    host = scout.domain_of(f"http://{domain}") or scout.domain_of(domain)
    if not host:
        raise HTTPException(400, "could not parse a domain")
    existing = (await session.execute(
        select(SiteProfile).where(SiteProfile.domain == host)
    )).scalars().first()
    if existing:
        return RedirectResponse(f"/scout/profiles/{existing.id}", status_code=303)
    profile = SiteProfile(
        domain=host,
        display_name=display_name.strip() or None,
        status=SiteProfileStatus.ACTIVE,
    )
    session.add(profile)
    await session.flush()
    # Start with an empty JSON-LD recipe the user can fill in.
    session.add(ExtractionRecipe(
        site_profile_id=profile.id, version=1,
        strategy=ExtractionStrategy.JSONLD, field_map={}, active=True,
    ))
    await session.commit()
    await session.refresh(profile)
    return RedirectResponse(f"/scout/profiles/{profile.id}", status_code=303)


async def _latest_learning_job(
    session: AsyncSession, profile_id: uuid.UUID
) -> SiteLearningJob | None:
    return (await session.execute(
        select(SiteLearningJob)
        .where(SiteLearningJob.site_profile_id == profile_id)
        .order_by(desc(SiteLearningJob.created_at))
        .limit(1)
    )).scalars().first()


async def _profile_context(session: AsyncSession, user: User, profile: SiteProfile, **extra) -> dict:
    recipes = sorted(profile.recipes, key=lambda r: r.version, reverse=True)
    active = next((r for r in recipes if r.active), None)
    ctx = {
        "user": user,
        "profile": profile,
        "recipes": recipes,
        "active": active,
        "strategies": [s.value for s in ExtractionStrategy],
        "latest_learning_job": await _latest_learning_job(session, profile.id),
    }
    ctx.update(extra)
    return ctx


@router.get("/scout/profiles/{profile_id}", response_class=HTMLResponse, response_model=None)
async def profile_detail(
    request: Request,
    profile_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    profile = await session.get(SiteProfile, profile_id)
    if not profile:
        raise HTTPException(404, "profile not found")
    return templates.TemplateResponse(
        request, "scout_profile.html",
        await _profile_context(session, user, profile),
    )


@router.post("/scout/profiles/{profile_id}/learn", response_class=HTMLResponse, response_model=None)
async def learn_recipe(
    profile_id: uuid.UUID,
    sample_url: str = Form(...),
    use_browser: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Queue an agent-assisted learning job: fetch the sample page, have a
    worker propose a recipe, validate it, and save it as the active recipe."""
    profile = await session.get(SiteProfile, profile_id)
    if not profile:
        raise HTTPException(404, "profile not found")
    url = sample_url.strip()
    if not url:
        raise HTTPException(400, "a sample URL is required")

    job = SiteLearningJob(
        site_profile_id=profile_id, status="running", sample_url=url,
        use_browser=bool(use_browser.strip()),
    )
    profile.status = SiteProfileStatus.LEARNING
    session.add(job)
    await session.commit()
    await session.refresh(job)

    # Fire-and-forget: rendering a JS page can take 10-40s, so we don't block
    # the POST on it. The job shows 'running' immediately and the profile page
    # auto-refreshes to track it; run_learning_job records any error itself.
    asyncio.create_task(scout.run_learning_job(job.id))
    return RedirectResponse(f"/scout/profiles/{profile_id}", status_code=303)


@router.post("/scout/recipes/{recipe_id}", response_class=HTMLResponse, response_model=None)
async def edit_recipe(
    recipe_id: uuid.UUID,
    strategy: str = Form(...),
    field_map: str = Form("{}"),
    needs_js: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    recipe = await session.get(ExtractionRecipe, recipe_id)
    if not recipe:
        raise HTTPException(404, "recipe not found")
    try:
        parsed_map = json.loads(field_map or "{}")
        if not isinstance(parsed_map, dict):
            raise ValueError("field map must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, f"invalid field map JSON: {exc}")
    try:
        recipe.strategy = ExtractionStrategy(strategy)
    except ValueError:
        raise HTTPException(400, f"unknown strategy: {strategy}")
    recipe.field_map = parsed_map
    recipe.needs_js = bool(needs_js.strip())
    await session.commit()
    return RedirectResponse(
        f"/scout/profiles/{recipe.site_profile_id}", status_code=303
    )


@router.post("/scout/profiles/{profile_id}/test-fetch", response_class=HTMLResponse, response_model=None)
async def test_fetch(
    request: Request,
    profile_id: uuid.UUID,
    url: str = Form(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    profile = await session.get(SiteProfile, profile_id)
    if not profile:
        raise HTTPException(404, "profile not found")
    # Run the real fetch path — this exercises the active recipe exactly as
    # the Operator's fetch_url tool would.
    result = await web_fetch.fetch(url.strip())
    return templates.TemplateResponse(
        request,
        "scout_profile.html",
        await _profile_context(
            session, user, profile, test_url=url.strip(), test_result=result,
        ),
    )


@router.post("/scout/profiles/{profile_id}/delete", response_class=HTMLResponse, response_model=None)
async def delete_profile(
    profile_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    profile = await session.get(SiteProfile, profile_id)
    if profile:
        await session.delete(profile)  # cascades to recipes
        await session.commit()
    return RedirectResponse("/scout", status_code=303)
