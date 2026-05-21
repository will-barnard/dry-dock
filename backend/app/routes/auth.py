"""Public auth routes: /setup (bootstrap), /login, /logout."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    fetch_user_by_email,
    has_any_users,
    hash_password,
    mark_users_exist,
    verify_password,
)
from app.db import get_session
from app.models import User

log = structlog.get_logger()
router = APIRouter(tags=["auth"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _render(request: Request, name: str, ctx: dict, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


# ── /setup: first-run bootstrap ───────────────────────────────────


@router.get("/setup", response_class=HTMLResponse, response_model=None)
async def setup_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse | RedirectResponse:
    if await has_any_users(session):
        return RedirectResponse("/login", status_code=303)
    return _render(request, "setup.html", {"error": None, "email": "", "name": ""})


@router.post("/setup", response_class=HTMLResponse, response_model=None)
async def setup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    name: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    if await has_any_users(session):
        return RedirectResponse("/login", status_code=303)

    email_norm = email.strip().lower()
    name_clean = name.strip() or None

    def err(msg: str, code: int = 400) -> HTMLResponse:
        return _render(
            request,
            "setup.html",
            {"error": msg, "email": email_norm, "name": name_clean or ""},
            status_code=code,
        )

    if not email_norm or "@" not in email_norm:
        return err("Email looks wrong.")
    if len(password) < 10:
        return err("Password must be at least 10 characters.")
    if password != confirm:
        return err("Passwords don't match.")

    user = User(
        email=email_norm,
        password_hash=hash_password(password),
        name=name_clean,
        is_admin=True,
        last_login_at=datetime.now(timezone.utc),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    mark_users_exist()
    log.info("auth.bootstrap_user_created", email=user.email)

    request.session["user_id"] = str(user.id)
    return RedirectResponse("/", status_code=303)


# ── /login + /logout ──────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse | RedirectResponse:
    if not await has_any_users(session):
        return RedirectResponse("/setup", status_code=303)
    return _render(request, "login.html", {"error": None, "email": ""})


@router.post("/login", response_class=HTMLResponse, response_model=None)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    if not await has_any_users(session):
        return RedirectResponse("/setup", status_code=303)

    email_norm = email.strip().lower()
    user = await fetch_user_by_email(session, email_norm)
    if not user or not verify_password(password, user.password_hash):
        return _render(
            request,
            "login.html",
            {"error": "Invalid email or password.", "email": email_norm},
            status_code=401,
        )

    user.last_login_at = datetime.now(timezone.utc)
    await session.commit()
    request.session["user_id"] = str(user.id)
    log.info("auth.login", email=user.email)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ── /login/token: temp-account token login ────────────────────────


@router.get("/login/token/{token}", response_model=None)
async def token_login(
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    result = await session.execute(select(User).where(User.temp_token == token))
    user = result.scalar_one_or_none()
    if not user or not user.is_temp:
        return RedirectResponse("/login", status_code=303)
    if user.token_expires_at and user.token_expires_at < datetime.now(timezone.utc):
        return RedirectResponse("/login?error=expired", status_code=303)
    user.last_login_at = datetime.now(timezone.utc)
    await session.commit()
    request.session["user_id"] = str(user.id)
    log.info("auth.token_login", user_id=str(user.id), name=user.name)
    return RedirectResponse("/", status_code=303)
