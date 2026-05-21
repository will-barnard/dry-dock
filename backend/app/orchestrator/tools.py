"""Operator tool registry + dispatcher.

The Operator's agentic mode exposes a small set of tools to the model. The
worker emits a ChatToolCallMsg when the model wants one; the orchestrator
runs it here and returns the text result. Keeping the registry server-side
(not on the worker) means one place for API keys, rate limits, and audit.

Each tool returns a (text_for_model, structured_payload) pair: the text is
fed back to the model, the payload is persisted on the TOOL transcript row
so the UI can render something richer than a blob.
"""
from __future__ import annotations

import json
from typing import Any

import structlog

from app.orchestrator import web_fetch, web_search

log = structlog.get_logger()


# ── tool schemas (OpenAI / Ollama function-calling format) ──────────

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web and get a ranked list of results (title, URL, "
            "snippet). Use this to find pages, then call fetch_url on the "
            "promising results to read their full content. Good for current "
            "info the model's training data is too old to know."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A focused search query that YOU compose from the "
                        "user's intent — do not just copy their message. "
                        "Pull out the key entities, add distinguishing "
                        "details (brand, model, year, size, 'price', site "
                        "names), and drop conversational filler. Run multiple "
                        "searches with different phrasings if the first is "
                        "weak. Example: user 'what's my old Ludwig worth?' → "
                        "query 'Ludwig Supraphonic 1970s 14x5 snare price reverb'."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}

FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": (
            "Fetch a single web page and return its readable text plus any "
            "structured data (JSON-LD, price/product meta tags). Use this on "
            "URLs returned by web_search to read prices, specs, article text, "
            "etc. One URL per call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The absolute http(s) URL to fetch.",
                },
            },
            "required": ["url"],
        },
    },
}

# The tool set offered in agentic mode.
OPERATOR_TOOLS: list[dict[str, Any]] = [WEB_SEARCH_TOOL, FETCH_URL_TOOL]


# Prepended as a system message in tools mode so the model uses the tools
# deliberately instead of doing one lazy verbatim search. Kept short — local
# models follow concise instructions better than long ones.
TOOLS_GUIDANCE = (
    "You have two web tools: web_search and fetch_url. When the user asks "
    "about current facts, prices, products, news, or anything your training "
    "data may be stale on:\n"
    "1. Compose a focused search query from the user's INTENT — extract the "
    "key entities and add distinguishing detail (brand, model, year, size, "
    "the word 'price', relevant site names). Never just paste the user's "
    "raw words.\n"
    "2. Call web_search, read the results, then fetch_url the most promising "
    "ones to get real page content (prices live on the page, not in snippets).\n"
    "3. Run additional searches with different phrasings if the first is thin.\n"
    "4. Answer from what you actually found, cite sources inline as [1], [2], "
    "and say so plainly if the data wasn't available."
)


# ── dispatch ────────────────────────────────────────────────────────


async def run_tool(name: str, arguments: dict[str, Any]) -> tuple[str, dict]:
    """Execute one tool call. Returns (text_for_model, structured_payload).
    Never raises — failures come back as text the model can react to."""
    if name == "web_search":
        query = str(arguments.get("query") or "").strip()
        if not query:
            return "web_search requires a non-empty 'query'.", {"error": "no query"}
        response = await web_search.search(query)
        if response is None:
            return (
                "Web search is unavailable right now (disabled, over budget, "
                "or the backend errored). Answer from your own knowledge.",
                {"query": query, "ok": False, "results": []},
            )
        text = web_search.format_results_for_prompt(response)
        payload = {
            "query": query,
            "ok": True,
            "results": [r.model_dump() for r in response.results],
            "elapsed_ms": response.elapsed_ms,
            "backend": response.backend,
        }
        return text, payload

    if name == "fetch_url":
        url = str(arguments.get("url") or "").strip()
        if not url:
            return "fetch_url requires a non-empty 'url'.", {"error": "no url"}
        text = await web_fetch.fetch(url)
        payload = {"url": url, "chars": len(text)}
        return text, payload

    log.warning("tools.unknown_tool", name=name)
    return (
        f"Unknown tool '{name}'. Available tools: web_search, fetch_url.",
        {"error": "unknown tool", "name": name},
    )
