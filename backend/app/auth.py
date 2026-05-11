"""Authentication helpers + the dependency every browser route uses.

Bootstrap model: on the very first run the `users` table is empty. Until a
user is created via `/setup`, every other route redirects to `/setup`. Once
the first admin exists, `/setup` becomes inaccessible and visitors get
`/login` instead.

Sessions are signed cookies (Starlette SessionMiddleware) — the user_id lives
in the cookie payload and is verified on every request. No server-side session
store needed for a single-replica deploy. If/when we scale out, swap to a
Postgres-backed session store with no API change.

The worker WebSocket at /ws/worker does NOT use this — workers authenticate
with the shared secret on the query string.
"""
from __future__ import annotations

import uuid

import bcrypt
import structlog
from fastapi import Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User

log = structlog.get_logger()


# ── password hashing ──────────────────────────────────────────────


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ── user-existence cache ──────────────────────────────────────────
# Hot path: every authed request would otherwise count the users table. Once
# we know users exist we never need to re-check (the only way back to zero is
# manual DB wipe, which deserves a restart).


_has_users_cache: bool = False


async def has_any_users(session: AsyncSession) -> bool:
    global _has_users_cache
    if _has_users_cache:
        return True
    count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    if count and count > 0:
        _has_users_cache = True
        return True
    return False


def mark_users_exist() -> None:
    """Called after the first user is created so subsequent requests skip the count."""
    global _has_users_cache
    _has_users_cache = True


# ── lookups ───────────────────────────────────────────────────────


async def fetch_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email.lower().strip()))
    return result.scalar_one_or_none()


# ── auth redirect plumbing ────────────────────────────────────────


class AuthRedirect(Exception):
    """Raised by `get_current_user` when the visitor needs to go elsewhere.

    Caught by an exception handler registered in main.py and converted into a
    303 redirect. We use an exception rather than returning a RedirectResponse
    so we can short-circuit deeply nested dependency chains.
    """

    def __init__(self, location: str) -> None:
        self.location = location


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    if not await has_any_users(session):
        raise AuthRedirect("/setup")

    user_id_raw = request.session.get("user_id")
    if not user_id_raw:
        raise AuthRedirect("/login")

    try:
        user_id = uuid.UUID(str(user_id_raw))
    except (ValueError, TypeError):
        request.session.clear()
        raise AuthRedirect("/login")

    user = await session.get(User, user_id)
    if user is None:
        request.session.clear()
        raise AuthRedirect("/login")
    return user
