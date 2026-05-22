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
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
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
    SiteLearningJob,
    SiteProfile,
    SiteProfileStatus,
)

log = structlog.get_logger()

# Pools to try for a Scout learning inference, in order.
_LEARN_POOLS = ("docs", "researcher", "planner", "coder")

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


# ════════════════════════ learning (Phase B) ═══════════════════════
#
# Point a learning job at a sample URL. We fetch the page, hand the model the
# structured-data candidates we find (JSON-LD, embedded JSON, price meta), and
# ask it to write an extraction recipe. We then VALIDATE the proposal by
# applying it to the same page; only a recipe that actually pulls fields gets
# saved + activated as a new version. Rides the WorkbenchRequest transport
# (kind="scout_learn") so no new worker/protocol code is needed.

_LEARN_SYSTEM = """\
You configure data-extraction recipes for websites. Given the structured data
found on a sample page, output a JSON recipe describing how to extract these
fields when present: title, price, currency, condition, year, seller, url,
description. Include ONLY fields you can actually locate in the data provided.

Pick a strategy:
- "jsonld": data is in a JSON-LD object. field_map values are dotted paths INTO
  that object, e.g. "offers.price" or "name". A list step takes its first item.
- "embedded_json": data is in an embedded JSON blob (e.g. __NEXT_DATA__).
  field_map values are dotted paths into it.
- "selectors": last resort — field_map values are CSS selectors.

Output ONLY a single JSON object in a ```json code block:

```json
{"strategy": "jsonld",
 "field_map": {"title": "name", "price": "offers.price", "currency": "offers.priceCurrency"},
 "needs_js": false}
```

Map a field only if you can see where its value lives in the provided data.
"""


def extract_candidate_signal(html: str) -> str:
    """Reduce a page to the structured-data candidates a model needs to write
    a recipe: JSON-LD objects, an embedded-JSON blob's shape, and price/OG
    meta tags. Bounded so it fits a local model's context."""
    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []

    if soup.title and soup.title.string:
        parts.append(f"PAGE TITLE: {soup.title.string.strip()}")

    ld = list(_iter_jsonld_objects(soup))
    if ld:
        parts.append("JSON-LD OBJECTS (each is a candidate for strategy 'jsonld'):")
        for obj in ld[:6]:
            parts.append(json.dumps(obj)[:2000])

    blob = _find_embedded_json(soup)
    if blob is not None:
        parts.append(
            "EMBEDDED JSON top-level keys: " + ", ".join(list(blob.keys())[:40])
        )
        parts.append("EMBEDDED JSON sample (truncated): " + json.dumps(blob)[:3000])

    metas: list[str] = []
    for m in soup.find_all("meta"):
        key = m.get("property") or m.get("name") or ""
        val = (m.get("content") or "").strip()
        if val and ("price" in key.lower() or key.lower() in ("og:title", "description")):
            metas.append(f"{key}: {val}")
    if metas:
        parts.append("META TAGS:\n" + "\n".join(metas[:20]))

    return "\n\n".join(parts)[:12000] or "(no structured data found on the page)"


def build_learn_messages(domain: str, signal: str) -> list[dict[str, str]]:
    user = (
        f"## Site\n{domain}\n\n"
        f"## Structured data found on the sample page\n{signal}\n\n"
        f"## Task\nProduce the extraction recipe JSON per the rules. "
        f"Output only the JSON object."
    )
    return [
        {"role": "system", "content": _LEARN_SYSTEM},
        {"role": "user", "content": user},
    ]


def _parse_recipe_json(text: str) -> dict | None:
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue
    return None


async def _pick_learn_worker():
    from app.orchestrator.registry import registry
    for pool in _LEARN_POOLS:
        workers = await registry.by_pool(pool)
        if not workers:
            continue
        idle = [w for w in workers if w.current_task_id is None]
        return (idle or workers)[0]
    return None


async def dispatch_site_learning(job_id: uuid.UUID) -> str | None:
    """Fetch the sample page, hand its structured-data candidates to a worker,
    and ask for a recipe. Returns None on success or an error string."""
    from app.orchestrator import web_fetch
    from app.orchestrator.protocol import WorkbenchRequestMsg

    async with SessionLocal() as session:
        job = await session.get(SiteLearningJob, job_id)
        if not job:
            return "learning job not found"
        profile = await session.get(SiteProfile, job.site_profile_id)
        domain = profile.domain if profile else ""
        url = job.sample_url

    html, error = await web_fetch.fetch_raw(url)
    if html is None:
        return f"could not fetch the sample page: {error}"

    signal = extract_candidate_signal(html)
    messages = build_learn_messages(domain, signal)

    async with SessionLocal() as session:
        async with session.begin():
            job = await session.get(SiteLearningJob, job_id)
            if job:
                job.sample_html = html

    worker = await _pick_learn_worker()
    if worker is None:
        return (
            "No worker is online in the docs / researcher / planner / coder "
            "pools to learn the recipe."
        )

    msg = WorkbenchRequestMsg(
        job_id=job_id, kind="scout_learn", model=None, messages=messages,
    )
    try:
        await worker.send(msg.model_dump(mode="json"))
    except Exception as exc:
        log.warning("scout.learn_dispatch_failed", job=str(job_id), error=str(exc))
        return f"failed to reach worker {worker.name}: {exc}"

    async with SessionLocal() as session:
        async with session.begin():
            job = await session.get(SiteLearningJob, job_id)
            if job:
                job.status = "running"
                job.worker_name = worker.name
    log.info("scout.learn_dispatched", job=str(job_id), worker=worker.name)
    return None


async def handle_site_learning_result(
    job_id: uuid.UUID, success: bool, content: str, error: str | None
) -> None:
    """Validate the model's proposed recipe against the stashed page and, if
    it extracts anything, save + activate it as a new recipe version."""
    async with SessionLocal() as session:
        async with session.begin():
            job = await session.get(SiteLearningJob, job_id)
            if not job:
                log.warning("scout.learn_result_no_job", job=str(job_id))
                return
            if not success:
                job.status = "error"
                job.error = error or "worker reported failure"
                return

            proposed = _parse_recipe_json(content or "")
            if not proposed:
                job.status = "error"
                job.error = "could not parse a recipe JSON from the response"
                job.result = {"raw": (content or "")[:4000]}
                return

            try:
                strategy = ExtractionStrategy(str(proposed.get("strategy") or "jsonld"))
            except ValueError:
                strategy = ExtractionStrategy.JSONLD
            field_map = proposed.get("field_map")
            if not isinstance(field_map, dict) or not field_map:
                job.status = "error"
                job.error = "proposed recipe has no usable field_map"
                job.result = {"proposed": proposed}
                return

            # Validate against the exact page the model analyzed.
            transient = SimpleNamespace(strategy=strategy, field_map=field_map)
            fields = apply_recipe(job.sample_html or "", transient)
            if not fields:
                job.status = "error"
                job.error = "the proposed recipe matched nothing on the sample page"
                # Keep the evidence so the UI can show WHY: what the model
                # proposed, and what structured data the page actually exposed.
                job.result = {
                    "proposed": proposed,
                    "candidate_signal": extract_candidate_signal(job.sample_html or ""),
                }
                return

            # Save as a new active recipe version; deactivate the rest.
            recipes = (await session.execute(
                select(ExtractionRecipe).where(
                    ExtractionRecipe.site_profile_id == job.site_profile_id
                )
            )).scalars().all()
            next_version = max((r.version for r in recipes), default=0) + 1
            for r in recipes:
                r.active = False
            session.add(ExtractionRecipe(
                site_profile_id=job.site_profile_id,
                version=next_version,
                strategy=strategy,
                field_map=field_map,
                needs_js=bool(proposed.get("needs_js")),
                confidence=min(1.0, len(fields) / 6.0),
                active=True,
                last_validated_at=datetime.now(timezone.utc),
            ))
            profile = await session.get(SiteProfile, job.site_profile_id)
            if profile:
                profile.status = SiteProfileStatus.ACTIVE
            job.status = "done"
            job.result = {
                "version": next_version,
                "strategy": strategy.value,
                "fields_found": list(fields.keys()),
                "sample": fields,
            }
    log.info("scout.learn_result_applied", job=str(job_id))
