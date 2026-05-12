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
        if row.key.startswith("role_model.") or row.key.startswith("worker_priority."):
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


# ── worker priority (per-worker integer; lower = preferred) ────────


_DEFAULT_WORKER_PRIORITY = 100


def _wp_key(name: str) -> str:
    return f"worker_priority.{name}"


async def get_worker_priority(name: str) -> int:
    """Return this worker's priority — lower value means more preferred. Default 100."""
    async with _cache_lock:
        if not _cache_loaded:
            async with SessionLocal() as session:
                await _load_cache(session)
        raw = _cache.get(_wp_key(name))
    if raw is None:
        return _DEFAULT_WORKER_PRIORITY
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_WORKER_PRIORITY


async def get_all_worker_priorities() -> dict[str, int]:
    """Snapshot of every priority currently configured. Workers not present
    here use the default; callers should default missing names to 100."""
    async with _cache_lock:
        if not _cache_loaded:
            async with SessionLocal() as session:
                await _load_cache(session)
        items = {k: v for k, v in _cache.items() if k.startswith("worker_priority.")}
    out: dict[str, int] = {}
    for key, value in items.items():
        name = key[len("worker_priority."):]
        try:
            out[name] = int(value)
        except (TypeError, ValueError):
            continue
    return out


async def set_worker_priority(name: str, priority: int | None) -> None:
    """Upsert a worker's priority. ``None`` clears the override back to default."""
    name = (name or "").strip()
    if not name:
        raise ValueError("worker name required")
    k = _wp_key(name)
    async with SessionLocal() as session:
        async with session.begin():
            if priority is None:
                existing = await session.get(Setting, k)
                if existing:
                    await session.delete(existing)
            else:
                stmt = pg_insert(Setting).values(key=k, value=str(int(priority)))
                stmt = stmt.on_conflict_do_update(
                    index_elements=[Setting.key], set_={"value": str(int(priority))}
                )
                await session.execute(stmt)
    async with _cache_lock:
        if priority is None:
            _cache.pop(k, None)
        else:
            _cache[k] = str(int(priority))
    log.info("settings.worker_priority_changed", worker=name, priority=priority)


async def workers_per_role() -> dict[str, list[dict]]:
    """For each role, a list of live worker descriptors with capability info.

    Includes ``priority`` so the settings page can render a per-worker tier.
    """
    priorities = await get_all_worker_priorities()
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
            "priority": priorities.get(worker.name, _DEFAULT_WORKER_PRIORITY),
        })
    # Sort by priority asc (primary first) so the UI shows the failover order.
    for role, lst in out.items():
        lst.sort(key=lambda d: (d["priority"], d["name"]))
    return out


def known_roles() -> Iterable[str]:
    return KNOWN_ROLES
