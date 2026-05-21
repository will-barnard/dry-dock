"""Settings page + API: per-role model assignment, password change, temp accounts."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, hash_password, verify_password
from app.db import get_session
from app.models import User
from app.orchestrator.settings_service import (
    KNOWN_ROLES,
    available_models_per_role,
    get_all_role_models,
    set_role_model,
    set_worker_priority,
    workers_per_role,
)

router = APIRouter(tags=["settings"])
api_router = APIRouter(prefix="/api/settings", tags=["settings"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── HTMX page ─────────────────────────────────────────────────────


@router.get("/settings", response_class=HTMLResponse, response_model=None)
async def settings_page(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
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
    temp_accounts = list((await session.execute(
        select(User).where(User.is_temp == True).order_by(User.created_at.desc())  # noqa: E712
    )).scalars().all())
    return templates.TemplateResponse(
        request, "settings.html", {
            "user": user,
            "rows": rows,
            "temp_accounts": temp_accounts,
            "now": datetime.now(timezone.utc),
        }
    )


@router.post("/settings", response_model=None)
async def settings_submit(
    request: Request,
    user: User = Depends(get_current_user),
) -> RedirectResponse:
    # Form arrives as `model.<role>=<value>` and `priority.<worker>=<int>` pairs.
    form = await request.form()
    for role in KNOWN_ROLES:
        field = f"model.{role}"
        if field in form:
            value = (form.get(field) or "").strip()
            await set_role_model(role, value or None)
    # Worker priority inputs — iterate ALL form keys so we pick up any worker
    # name without having to know the list ahead of time.
    for key in form.keys():
        if key.startswith("priority."):
            worker_name = key[len("priority."):]
            raw = (form.get(key) or "").strip()
            try:
                prio = int(raw)
            except (ValueError, TypeError):
                prio = None
            await set_worker_priority(worker_name, prio)
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


# ── Change password ───────────────────────────────────────────────


@router.post("/settings/password", response_model=None)
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if not verify_password(current_password, user.password_hash):
        return RedirectResponse("/settings?error=wrong_password", status_code=303)
    if len(new_password) < 10:
        return RedirectResponse("/settings?error=too_short", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse("/settings?error=mismatch", status_code=303)
    user.password_hash = hash_password(new_password)
    await session.commit()
    return RedirectResponse("/settings?success=password_changed", status_code=303)


# ── Temp accounts ─────────────────────────────────────────────────

_TOKEN_TTL_HOURS = 12


@router.post("/settings/temp-account", response_model=None)
async def create_temp_account(
    request: Request,
    label: str = Form(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if not user.is_admin:
        raise HTTPException(403, "Admin only")
    token = secrets.token_hex(32)
    placeholder_email = f"temp-{secrets.token_hex(8)}@temp.local"
    expires = datetime.now(timezone.utc) + timedelta(hours=_TOKEN_TTL_HOURS)
    temp_user = User(
        email=placeholder_email,
        password_hash="!",  # unusable password — login only via token
        name=label.strip()[:255] or "Temp user",
        is_admin=False,
        is_temp=True,
        temp_token=token,
        token_expires_at=expires,
    )
    session.add(temp_user)
    await session.commit()
    await session.refresh(temp_user)
    return RedirectResponse(f"/settings?new_token={token}", status_code=303)


@router.post("/settings/temp-account/{user_id}/delete", response_model=None)
async def delete_temp_account(
    request: Request,
    user_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if not user.is_admin:
        raise HTTPException(403, "Admin only")
    import uuid as _uuid
    try:
        uid = _uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(400, "Invalid user ID")
    target = await session.get(User, uid)
    if target and target.is_temp:
        await session.delete(target)
        await session.commit()
    return RedirectResponse("/settings", status_code=303)
