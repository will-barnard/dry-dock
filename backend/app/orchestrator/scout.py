"""Scout — site-knowledge module (Phase A).

Stores, per domain, a validated "recipe" for extracting structured fields
from that site, so the Operator's fetch_url can return clean fields (price,
title, condition) instead of a wall of page text. Phase A is the runtime
path + hand-written recipes; agent-assisted learning is Phase B.

This module owns:
  - the recipe applier (apply_recipe) used by web_fetch
  - profile lookup helpers
  - the idempotent Reverb starter seed
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import structlog
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import (
    ExtractionRecipe,
    ExtractionStrategy,
    SiteProfile,
    SiteProfileStatus,
)

log = structlog.get_logger()

# Fields we try to surface, in display order.
KNOWN_FIELDS = ("title", "price", "currency", "condition", "year", "seller", "url", "description")

# Common containers for embedded-JSON state blobs.
_EMBEDDED_CONTAINERS = ("__NEXT_DATA__", "__NUXT__", "__APOLLO_STATE__")


# ── helpers ─────────────────────────────────────────────────────────


def domain_of(url: str) -> str | None:
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _resolve_path(obj: Any, dotted: str) -> Any:
    """Walk a dotted path through nested dicts/lists. A list step takes the
    first element (marketplace 'offers' is often a one-element list)."""
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, list):
            cur = cur[0] if cur else None
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    # If we land on a list, take the first scalar-ish element.
    if isinstance(cur, list):
        cur = cur[0] if cur else None
    return cur


def _iter_jsonld_objects(soup: BeautifulSoup):
    """Yield every JSON-LD object on the page, flattening @graph arrays."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                if "@graph" in item and isinstance(item["@graph"], list):
                    for g in item["@graph"]:
                        if isinstance(g, dict):
                            yield g
                else:
                    yield item


def _find_embedded_json(soup: BeautifulSoup) -> dict | None:
    """Find a likely embedded-state JSON blob (__NEXT_DATA__ etc.)."""
    # Framework script ids first.
    for cid in _EMBEDDED_CONTAINERS:
        tag = soup.find("script", id=cid) or soup.find("script", attrs={"name": cid})
        if tag and tag.string:
            try:
                return json.loads(tag.string)
            except (json.JSONDecodeError, ValueError):
                pass
    # Any application/json script as a fallback.
    for tag in soup.find_all("script", attrs={"type": "application/json"}):
        if tag.string:
            try:
                parsed = json.loads(tag.string)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                continue
    return None


# ── recipe applier ─────────────────────────────────────────────────


def apply_recipe(html: str, recipe: ExtractionRecipe) -> dict[str, Any]:
    """Apply a recipe to page HTML and return whatever structured fields it
    resolves. Empty dict means the recipe matched nothing (caller should fall
    back to generic extraction)."""
    soup = BeautifulSoup(html, "html.parser")
    field_map: dict[str, str] = recipe.field_map or {}
    out: dict[str, Any] = {}

    if recipe.strategy == ExtractionStrategy.JSONLD:
        # Prefer a JSON-LD object that actually contains the mapped paths;
        # Product/Offer pages can carry several blocks (BreadcrumbList, etc.).
        best: dict | None = None
        best_hits = 0
        for obj in _iter_jsonld_objects(soup):
            hits = sum(
                1 for path in field_map.values()
                if _resolve_path(obj, path) is not None
            )
            if hits > best_hits:
                best, best_hits = obj, hits
        if best is not None:
            for field, path in field_map.items():
                val = _resolve_path(best, path)
                if val is not None:
                    out[field] = val

    elif recipe.strategy == ExtractionStrategy.EMBEDDED_JSON:
        blob = _find_embedded_json(soup)
        if blob is not None:
            for field, path in field_map.items():
                val = _resolve_path(blob, path)
                if val is not None:
                    out[field] = val

    elif recipe.strategy == ExtractionStrategy.SELECTORS:
        for field, selector in field_map.items():
            try:
                el = soup.select_one(selector)
            except Exception:
                el = None
            if el is not None:
                text = el.get_text(strip=True)
                if text:
                    out[field] = text

    # ExtractionStrategy.API isn't applied to HTML — it's a Phase C fetch path.
    # Normalize scalar values to strings for clean display.
    return {k: (v if isinstance(v, (str, int, float)) else json.dumps(v)) for k, v in out.items()}


def format_fields(domain: str, url: str, fields: dict[str, Any]) -> str:
    """Render extracted fields as a compact block for the model."""
    lines = [f"Fetched via {domain} recipe: {url}", ""]
    for key in KNOWN_FIELDS:
        if key in fields:
            lines.append(f"{key}: {fields[key]}")
    # Include any extra fields the recipe defined beyond the known set.
    for key, val in fields.items():
        if key not in KNOWN_FIELDS:
            lines.append(f"{key}: {val}")
    return "\n".join(lines)


# ── lookup ──────────────────────────────────────────────────────────


async def active_recipe_for_domain(
    session: AsyncSession, domain: str
) -> tuple[SiteProfile, ExtractionRecipe] | None:
    """Return (profile, active_recipe) for a domain, or None."""
    profile = (await session.execute(
        select(SiteProfile).where(SiteProfile.domain == domain)
    )).scalars().first()
    if profile is None:
        return None
    recipe = next((r for r in profile.recipes if r.active), None)
    if recipe is None:
        return None
    return profile, recipe


async def record_outcome(recipe_id, *, success: bool) -> None:
    """Bump success/failure counters on a recipe after a fetch attempt."""
    async with SessionLocal() as session:
        async with session.begin():
            recipe = await session.get(ExtractionRecipe, recipe_id)
            if recipe is None:
                return
            if success:
                recipe.success_count += 1
            else:
                recipe.failure_count += 1


# ── seed ────────────────────────────────────────────────────────────


# A hand-written starter recipe for Reverb. JSON-LD is the most likely place
# Reverb exposes listing data in the server-rendered HTML. This is a STARTING
# POINT — use Scout's "Test fetch" to validate it against a real listing and
# edit the field map if the paths differ.
_REVERB_FIELD_MAP = {
    "title": "name",
    "description": "description",
    "price": "offers.price",
    "currency": "offers.priceCurrency",
    "condition": "offers.itemCondition",
    "url": "offers.url",
}


async def seed_reverb_profile() -> None:
    """Create the Reverb starter profile + recipe if no profile exists yet.
    Idempotent — safe to call on every boot."""
    async with SessionLocal() as session:
        async with session.begin():
            existing = (await session.execute(
                select(SiteProfile).where(SiteProfile.domain == "reverb.com")
            )).scalars().first()
            if existing is not None:
                return
            profile = SiteProfile(
                domain="reverb.com",
                display_name="Reverb",
                status=SiteProfileStatus.ACTIVE,
                notes=(
                    "Starter recipe — JSON-LD Product/Offer. Validate with "
                    "Test fetch on a real listing and adjust the field map if "
                    "the paths differ."
                ),
            )
            session.add(profile)
            await session.flush()
            session.add(ExtractionRecipe(
                site_profile_id=profile.id,
                version=1,
                strategy=ExtractionStrategy.JSONLD,
                field_map=_REVERB_FIELD_MAP,
                search_strategy={
                    "type": "url",
                    "pattern": "https://reverb.com/marketplace?query={q}",
                },
                needs_js=False,
                confidence=0.5,
                active=True,
            ))
    log.info("scout.seeded_reverb_profile")
