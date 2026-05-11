"""Helpers for reading and writing app-wide settings.

Two views the rest of the codebase needs:

- "What model should I use for role X?" — read-through to the `app_settings`
  table, falling back to env-derived defaults. Cached so dispatch doesn't
  query the DB on every claim_grant.

- "Which models are available for role X right now?" — aggregated view of
  the `installed_models` field across every live worker in that pool.

The known roles live alongside the dispatcher's KNOWN_POOLS — they're the
same list. Kept here so importers don't pull dispatcher.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.models import Setting
from app.orchestrator.registry import registry

log = structlog.get_logger()

KNOWN_ROLES: tuple[str, ...] = (
    "planner",
    "coder",
    "reviewer",
    "tester",
    "refactorer",
    "docs",
    "researcher",
)


def _key(role: str) -> str:
    return f"role_model.{role}"


# ── env-derived fallbacks ──────────────────────────────────────────


def _env_default_for(role: str) -> str:
    s = get_settings()
    if role == "planner":
        return s.default_planner_model
    return s.default_code_model


# ── module-level cache ─────────────────────────────────────────────


_cache: dict[str, str] = {}
_cache_lock = asyncio.Lock()
_cache_loaded = False


async def _load_cache(session: AsyncSession) -> None:
    global _cache_loaded
    rows = (await session.execute(select(Setting))).scalars().all()
    new_cache: dict[str, str] = {}
    for row in rows:
        if row.key.startswith("role_model."):
            new_cache[row.key] = row.value
    _cache.clear()
    _cache.update(new_cache)
    _cache_loaded = True


async def get_role_model(role: str) -> str:
    """Return the configured model for a role, or the env default if unset."""
    async with _cache_lock:
        if not _cache_loaded:
            async with SessionLocal() as session:
                await _load_cache(session)
        v = _cache.get(_key(role))
    return v or _env_default_for(role)


async def get_all_role_models() -> dict[str, str]:
    async with _cache_lock:
        if not _cache_loaded:
            async with SessionLocal() as session:
                await _load_cache(session)
    out: dict[str, str] = {}
    for role in KNOWN_ROLES:
        out[role] = _cache.get(_key(role)) or _env_default_for(role)
    return out


async def set_role_model(role: str, model: str | None) -> None:
    """Upsert (or clear, if model is None/empty) the role assignment."""
    if role not in KNOWN_ROLES:
        raise ValueError(f"unknown role: {role}")
    k = _key(role)
    async with SessionLocal() as session:
        async with session.begin():
            if not model:
                existing = await session.get(Setting, k)
                if existing:
                    await session.delete(existing)
            else:
                stmt = pg_insert(Setting).values(key=k, value=model)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[Setting.key], set_={"value": model}
                )
                await session.execute(stmt)
    async with _cache_lock:
        if model:
            _cache[k] = model
        else:
            _cache.pop(k, None)
    log.info("settings.role_model_changed", role=role, model=model)


# ── availability from live workers ─────────────────────────────────


async def available_models_per_role() -> dict[str, list[str]]:
    """For each role, return the deduplicated, sorted list of models
    currently installed on at least one online worker in that pool."""
    out: dict[str, set[str]] = {role: set() for role in KNOWN_ROLES}
    for worker in await registry.all():
        if worker.pool not in out:
            continue
        for m in worker.installed_models or ():
            out[worker.pool].add(m)
    return {role: sorted(models) for role, models in out.items()}


async def workers_per_role() -> dict[str, list[dict]]:
    """For each role, a list of live worker descriptors with capability info."""
    out: dict[str, list[dict]] = {role: [] for role in KNOWN_ROLES}
    for worker in await registry.all():
        if worker.pool not in out:
            continue
        out[worker.pool].append({
            "name": worker.name,
            "hardware_class": worker.hardware_class,
            "installed_models": list(worker.installed_models or ()),
            "ram_gb": worker.ram_gb,
            "max_context": worker.max_context,
            "gpu_vram_gb": worker.gpu_vram_gb,
            "gpu_model": worker.gpu_model,
            "current_task_id": str(worker.current_task_id) if worker.current_task_id else None,
        })
    return out


def known_roles() -> Iterable[str]:
    return KNOWN_ROLES
