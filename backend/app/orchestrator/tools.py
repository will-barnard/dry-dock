"""Pilot tool registry + dispatcher.

Pilot's agentic mode exposes a small set of tools to the model. The
worker emits a ChatToolCallMsg when the model wants one; the orchestrator
runs it here and returns the text result. Keeping the registry server-side
(not on the worker) means one place for API keys, rate limits, and audit.

Each tool returns a (text_for_model, structured_payload) pair: the text is
fed back to the model, the payload is persisted on the TOOL transcript row
so the UI can render something richer than a blob.
"""
from __future__ import annotations

import json
import uuid
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
                "site": {
                    "type": "string",
                    "description": (
                        "Optional. Restrict results to a single domain, e.g. "
                        "'reverb.com'. Use when the user wants results from a "
                        "specific site. (A conversation-level restriction, if "
                        "set, overrides this.)"
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

GENERATE_IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "Generate an image from a text description, rendered locally on "
            "the user's own GPU. Use this when the user asks you to draw, "
            "make, render, or show them a picture of something. Write a "
            "detailed visual prompt yourself — subject, setting, lighting, "
            "composition, style — rather than passing the user's words "
            "through. Takes 10-30 seconds. One image per call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "A dense, comma-separated visual description that YOU "
                        "compose: subject, setting, lighting, lens or "
                        "composition, art style. Example: user 'draw my "
                        "Rhodes' → 'a vintage Rhodes electric piano, worn "
                        "tolex case, warm studio lighting, shallow depth of "
                        "field, 50mm photograph'."
                    ),
                },
                "negative_prompt": {
                    "type": "string",
                    "description": (
                        "Optional. What to avoid, comma-separated, e.g. "
                        "'blurry, watermark, text, distorted'."
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
}

# The tool set offered in agentic mode.
PILOT_TOOLS: list[dict[str, Any]] = [WEB_SEARCH_TOOL, FETCH_URL_TOOL]


async def available_tools() -> list[dict[str, Any]]:
    """The tool list for THIS turn.

    `generate_image` is offered only when an imager is online and idle, and
    the reason is the shape of the chat loop: a tool call blocks the worker's
    turn while it runs. Offering the tool while the GPU box is asleep would
    invite the model to call something that needs a 60-150s wake, stalling the
    conversation and pinning a text worker for the duration. Better that the
    tool simply isn't there — the model then answers normally and the user can
    use the Darkroom module, which handles waking gracefully.
    """
    tools = list(PILOT_TOOLS)
    try:
        from app.orchestrator.image_jobs import pick_imager

        worker = await pick_imager()
        if worker is not None and not worker.current_image_jobs:
            tools.append(GENERATE_IMAGE_TOOL)
    except Exception:  # noqa: BLE001 — never let this break plain chat
        log.warning("tools.imager_probe_failed")
    return tools


# Prepended as a system message in tools mode. Directive about DEPTH (fetch
# several real sources, not one snippet) and SYNTHESIS (give a concrete
# answer, not a list of links) — the two things local models skimp on.
TOOLS_GUIDANCE = (
    "You are a research assistant with two web tools: web_search and "
    "fetch_url. Go DEEP and SYNTHESIZE — a good answer is grounded in several "
    "real pages you actually read, and ends with a concrete conclusion, not a "
    "list of links.\n"
    "\n"
    "For any question about current facts, prices, products, or news:\n"
    "1. Compose a focused search query from the user's INTENT — key entities "
    "plus distinguishing detail (brand, model, year, size, the word 'price', "
    "relevant sites). Never paste the user's raw words.\n"
    "2. web_search, then read the results.\n"
    "3. fetch_url at least 2-3 of the most relevant results to read the actual "
    "page. Snippets are NOT enough — prices, specs, and details live on the "
    "page. Do NOT answer a pricing or factual question from snippets alone.\n"
    "4. If results are thin, conflicting, or you have fewer than a couple of "
    "solid sources, search again with different phrasing and fetch more.\n"
    "5. Only then answer, and SYNTHESIZE rather than list links:\n"
    "   • Price/value questions: give a RANGE (lowest, typical, highest) from "
    "the listings you actually read, and note condition or variation.\n"
    "   • Comparisons/recommendations: state a clear conclusion and the "
    "reasoning behind it.\n"
    "   • Always cite the sources you used inline as [1], [2], … and say "
    "plainly when data wasn't available rather than guessing.\n"
    "\n"
    "If a generate_image tool is offered, you can also make pictures: call it "
    "when the user asks you to draw, render, or show them something, writing "
    "a detailed visual prompt yourself. The image is displayed to the user "
    "automatically — never say you are unable to show images. If that tool is "
    "not in your list, image generation is unavailable this turn; say so "
    "plainly instead of pretending."
)


# ── dispatch ────────────────────────────────────────────────────────


async def run_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    conversation_id: uuid.UUID | None = None,
) -> tuple[str, dict]:
    """Execute one tool call. Returns (text_for_model, structured_payload).
    Never raises — failures come back as text the model can react to."""
    if name == "web_search":
        query = str(arguments.get("query") or "").strip()
        if not query:
            return "web_search requires a non-empty 'query'.", {"error": "no query"}
        site = str(arguments.get("site") or "").strip() or None
        response = await web_search.search(query, site=site)
        if response is None:
            return (
                "Web search is unavailable right now (disabled, over budget, "
                "or the backend errored). Answer from your own knowledge.",
                {"query": query, "site": site, "ok": False, "results": []},
            )
        text = web_search.format_results_for_prompt(response)
        payload = {
            "query": query,
            "site": site,
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

    if name == "generate_image":
        return await _run_generate_image(arguments, conversation_id)

    log.warning("tools.unknown_tool", name=name)
    return (
        f"Unknown tool '{name}'. Available tools: web_search, fetch_url, "
        "generate_image.",
        {"error": "unknown tool", "name": name},
    )


# How long a chat turn will sit on a render before handing the conversation
# back. The tool is only offered when an imager is idle, so this covers a warm
# render comfortably; anything slower gets a "check Darkroom" answer instead of
# an indefinitely stalled turn.
_IMAGE_TOOL_WAIT_SECONDS = 90.0


async def _run_generate_image(
    arguments: dict[str, Any], conversation_id: uuid.UUID | None
) -> tuple[str, dict]:
    """Render an image inside a chat turn.

    The model only ever sees text, so the image comes back as a URL it can
    cite; the structured payload carries the same URL so the transcript can
    render the picture inline.
    """
    from app.config import get_settings
    from app.models import ImageJobSource, ImageJobStatus
    from app.orchestrator.image_jobs import ImageError, submit_job, wait_for_job

    prompt = str(arguments.get("prompt") or "").strip()
    if not prompt:
        return "generate_image requires a non-empty 'prompt'.", {"error": "no prompt"}
    negative = str(arguments.get("negative_prompt") or "").strip() or None

    try:
        job = await submit_job(
            prompt,
            negative_prompt=negative,
            params={"batch": 1},
            source=ImageJobSource.OPERATOR,
            conversation_id=conversation_id,
        )
    except ImageError as exc:
        return (
            f"Image generation is unavailable right now: {exc} Tell the user "
            "plainly rather than trying again.",
            {"ok": False, "error": str(exc)},
        )

    finished = await wait_for_job(job.id, _IMAGE_TOOL_WAIT_SECONDS)
    base = get_settings().drydock_base_url.rstrip("/")

    if finished is None:
        return "The image job disappeared before it finished.", {"ok": False}

    if finished.status is ImageJobStatus.DONE:
        images = (finished.result or {}).get("images") or []
        index = images[0].get("index", 0) if images else 0
        url = f"{base}/darkroom/images/{finished.id}/{index}.png"
        return (
            f"Image generated successfully. It is shown to the user above this "
            f"message; the URL is {url}. Briefly describe what you made — do "
            f"not paste the URL again or claim you cannot show images.",
            {
                "ok": True,
                "job_id": str(finished.id),
                "url": url,
                "thumb_url": f"{base}/darkroom/images/{finished.id}/{index}_thumb.webp",
                "prompt": prompt,
                "seed": images[0].get("seed") if images else None,
                "checkpoint": (finished.result or {}).get("checkpoint"),
            },
        )

    if finished.status is ImageJobStatus.ERROR:
        return (
            f"Image generation failed: {finished.error}",
            {"ok": False, "job_id": str(finished.id), "error": finished.error},
        )

    return (
        "The image is still rendering — it will appear in the Darkroom module "
        f"({base}/darkroom) shortly. Tell the user that and move on.",
        {"ok": False, "job_id": str(finished.id), "status": finished.status.value,
         "pending": True},
    )
