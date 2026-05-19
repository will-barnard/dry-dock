"""Web search backend for the Operator module.

Phase 1 architecture: the orchestrator (not the worker) runs the search
before each Operator turn that has `Conversation.web_search_enabled` set.
The top results are folded into the prompt as a synthetic system message
so the model can ground its answer in current information, and persisted
as a TOOL-role ConversationMessage for the transcript UI.

`WebSearchProvider` is a thin protocol so we can swap SearXNG for Tavily
or anything else without touching `chat.py`. Picking a backend at import
time keeps the hot path allocation-free.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx
import structlog
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.db import SessionLocal
from app.models import WebSearchUsage

log = structlog.get_logger()


# ── shapes ─────────────────────────────────────────────────────────


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    score: float | None = None


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    elapsed_ms: int
    backend: str


# ── provider interface + implementations ───────────────────────────


class WebSearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, max_results: int) -> SearchResponse: ...


class SearXNGProvider:
    """Calls a self-hosted SearXNG instance's JSON API.

    SearXNG aggregates Google/Bing/DDG/etc. without leaking the query to any
    one engine. To get JSON output, the instance must have `formats: ['json']`
    in its settings.yml (it's NOT on by default).
    """

    name = "searxng"

    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def search(self, query: str, *, max_results: int) -> SearchResponse:
        started = datetime.now(timezone.utc)
        params = {
            "q": query,
            "format": "json",
            # General category — broad enough to surface news + docs + Q&A
            # without dragging in image / video / map results we can't use.
            "categories": "general",
            "safesearch": "1",
        }
        # follow_redirects: SearXNG instances behind a reverse proxy almost
        # always 301 http → https. Without this, the first request fails and
        # we silently degrade to "search unavailable" — surprising and
        # invisible. Same-host redirects are safe; httpx caps the chain at 20.
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            resp = await client.get(f"{self.base_url}/search", params=params)
            resp.raise_for_status()
            payload = resp.json()

        raw_results: list[dict[str, Any]] = payload.get("results", []) or []
        results: list[SearchResult] = []
        for r in raw_results[:max_results]:
            url = r.get("url") or ""
            title = (r.get("title") or "").strip()
            # SearXNG calls the snippet `content`. Some engines occasionally
            # ship it as `pretty_url` etc; fall through gracefully.
            snippet = (r.get("content") or "").strip()
            if not url or not title:
                continue
            results.append(SearchResult(
                title=title, url=url, snippet=snippet,
                score=float(r["score"]) if isinstance(r.get("score"), (int, float)) else None,
            ))

        elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        return SearchResponse(
            query=query, results=results, elapsed_ms=elapsed_ms, backend=self.name,
        )


# ── factory ────────────────────────────────────────────────────────


_provider_cache: WebSearchProvider | None = None


def get_provider() -> WebSearchProvider | None:
    """Return the configured provider, or None if web search is disabled or
    misconfigured. Cached after first call."""
    global _provider_cache
    if _provider_cache is not None:
        return _provider_cache

    settings = get_settings()
    if not settings.web_search_enabled:
        return None

    backend = (settings.web_search_backend or "").lower()
    if backend == "searxng":
        if not settings.searxng_url:
            log.warning("web_search.searxng_no_url")
            return None
        _provider_cache = SearXNGProvider(
            settings.searxng_url, settings.web_search_timeout_seconds
        )
        return _provider_cache

    log.warning("web_search.unknown_backend", backend=backend)
    return None


# ── budget tracking ────────────────────────────────────────────────


def _today_utc_midnight() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def get_usage_today() -> int:
    """Return the running search count for the current UTC day."""
    today = _today_utc_midnight()
    async with SessionLocal() as session:
        row = (await session.execute(
            select(WebSearchUsage).where(WebSearchUsage.day == today)
        )).scalars().first()
        return row.count if row else 0


async def _increment_usage() -> int:
    """Atomically bump today's usage counter and return the new value.

    Postgres UPSERT — single round-trip, no race window even with concurrent
    turns. The unique key on `day` makes ON CONFLICT meaningful.
    """
    today = _today_utc_midnight()
    async with SessionLocal() as session:
        async with session.begin():
            stmt = (
                pg_insert(WebSearchUsage)
                .values(day=today, count=1)
                .on_conflict_do_update(
                    index_elements=["day"],
                    set_={"count": WebSearchUsage.count + 1},
                )
                .returning(WebSearchUsage.count)
            )
            result = await session.execute(stmt)
            return int(result.scalar_one())


class BudgetExceeded(Exception):
    """Raised when the per-day search cap has been reached."""


async def check_and_charge_budget() -> int:
    """Atomically charge the budget. Returns the new count, or raises
    BudgetExceeded if the cap was already hit (or hit by this call).

    We charge first and then check — simpler than a check-then-charge that
    could race. On exceed the call is still counted, but subsequent calls
    will keep raising until midnight UTC, which is the intended behaviour.
    """
    settings = get_settings()
    new_count = await _increment_usage()
    if settings.web_search_daily_budget > 0 and new_count > settings.web_search_daily_budget:
        raise BudgetExceeded(
            f"daily web search cap of {settings.web_search_daily_budget} reached"
        )
    return new_count


# ── public API: one call from chat.py ──────────────────────────────


async def search(query: str) -> SearchResponse | None:
    """Run one web search through the configured provider. Returns None if
    web search is disabled, misconfigured, the budget is exhausted, or the
    backend errored out — the caller treats every failure as "no results"
    and proceeds without injection."""
    provider = get_provider()
    if provider is None:
        return None

    try:
        await check_and_charge_budget()
    except BudgetExceeded as exc:
        log.warning("web_search.budget_exceeded", reason=str(exc))
        return None

    settings = get_settings()
    try:
        return await asyncio.wait_for(
            provider.search(query, max_results=settings.web_search_max_results),
            timeout=settings.web_search_timeout_seconds + 1.0,
        )
    except asyncio.TimeoutError:
        log.warning("web_search.timeout", query=query[:120])
        return None
    except httpx.HTTPError as exc:
        log.warning("web_search.http_error", error=str(exc), query=query[:120])
        return None
    except Exception:  # noqa: BLE001
        log.exception("web_search.unexpected_error", query=query[:120])
        return None


# ── prompt formatting ─────────────────────────────────────────────


def format_results_for_prompt(response: SearchResponse) -> str:
    """Render the search results as a system-message payload the model can
    cite. Numbered list keeps [1]/[2]-style inline references unambiguous."""
    if not response.results:
        return (
            f"Web search ran for the user's question but returned no results "
            f"(query: \"{response.query}\"). Answer from your own knowledge "
            f"and tell the user the search came up empty."
        )
    lines: list[str] = [
        "Web context — fresh results from a web search the user requested.",
        "Use these to ground your answer in current information. Cite them "
        "inline with [1], [2], etc. Don't invent details beyond the snippets.",
        f"If the snippets don't answer the question, say so.",
        "",
        f"Query: \"{response.query}\"",
        "",
    ]
    for i, r in enumerate(response.results, start=1):
        lines.append(f"[{i}] {r.title}")
        lines.append(f"    {r.url}")
        if r.snippet:
            lines.append(f"    {r.snippet}")
        lines.append("")
    return "\n".join(lines).rstrip()
