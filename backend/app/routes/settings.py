"""Settings page + API: per-role model assignment.

`/settings` renders an HTMX page with one row per known role. Each row shows
the currently-configured model plus a dropdown of every model installed on at
least one online worker in that role's pool. Saving issues a POST that
upserts the row in `app_settings`.

There's also a tiny JSON API at `/api/settings/role-models` for programmatic
use (and for the architecture spec to stay testable without scraping HTML).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user
from app.models import User
from app.orchestrator.settings_service import (
    KNOWN_ROLES,
    available_models_per_role,
    get_all_role_models,
    set_role_model,
    workers_per_role,
)

router = APIRouter(tags=["settings"])
api_router = APIRouter(prefix="/api/settings", tags=["settings"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── HTMX page ─────────────────────────────────────────────────────


@router.get("/settings", response_class=HTMLResponse, response_model=None)
async def settings_page(
    request: Request, user: User = Depends(get_current_user)
) -> HTMLResponse:
    current = await get_all_role_models()
    available = await available_models_per_role()
    workers = await workers_per_role()
    rows = []
    for role in KNOWN_ROLES:
        rows.append({
            "role": role,
            "current": current[role],
            "available": available.get(role, []),
            "workers": workers.get(role, []),
        })
    return templates.TemplateResponse(
        request, "settings.html", {"user": user, "rows": rows}
    )


@router.post("/settings", response_model=None)
async def settings_submit(
    request: Request,
    user: User = Depends(get_current_user),
) -> RedirectResponse:
    # Form arrives as `model.<role>=<value>` pairs. We process the whole batch
    # in one shot so the page never lands in a partially-saved state.
    form = await request.form()
    for role in KNOWN_ROLES:
        field = f"model.{role}"
        if field in form:
            value = (form.get(field) or "").strip()
            # An empty value clears the role override and falls back to env default.
            await set_role_model(role, value or None)
    return RedirectResponse("/settings", status_code=303)


# ── JSON API ──────────────────────────────────────────────────────


@api_router.get("/role-models")
async def get_role_models_json() -> dict:
    return {
        "current": await get_all_role_models(),
        "available": await available_models_per_role(),
    }


@api_router.put("/role-models/{role}")
async def set_role_model_json(role: str, model: str = Form(default="")) -> dict:
    await set_role_model(role, model.strip() or None)
    return {"role": role, "model": model.strip() or None}
