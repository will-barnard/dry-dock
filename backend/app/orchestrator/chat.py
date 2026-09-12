"""Pilot chat dispatch + streaming accumulator.

Chat is deliberately NOT routed through the Task system. A conversation turn:

  1. The route persists the user message + an empty assistant message.
  2. `dispatch_turn` resolves the conversation's MODE to a (pool, model) pair
     via orchestrator/pilot.py, picks a live worker in that pool, and sends a
     `chat_request` over its WebSocket. The resolution happens here, per turn,
     and never at conversation-creation time — see pilot.py for why.
  3. The worker streams `chat_chunk` deltas; `on_chunk` accumulates them in
     memory and republishes the *running full text* on the conversation's
     EventBus topic (so the SSE handler can just replace a div).
  4. `chat_done` persists the final assistant content; `chat_error` records
     the failure on the assistant message.

In-memory accumulation is fine for a single-replica orchestrator. If we ever
run multiple replicas, the worker's WS lands on one replica and the SSE
subscriber must be on the same one — which it is, since the browser holds a
sticky connection. The DB row is the durable record either way.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog

from app.db import SessionLocal
from app.models import Conversation, ConversationMessage, MessageRole
from app.orchestrator.event_bus import bus
from app.orchestrator import pilot
from app.orchestrator.protocol import ChatRequestMsg
from app.orchestrator.registry import registry
from app.orchestrator import web_search
from app.orchestrator.tools import TOOLS_GUIDANCE, available_tools

log = structlog.get_logger()

# Running text per in-flight assistant message. Keyed by assistant_message_id.
# Populated by on_chunk, drained by on_done / on_error.
_accumulators: dict[uuid.UUID, str] = {}

# What each in-flight turn actually resolved to: {"worker", "model", "label"}.
# Kept because Pilot hides the model from the UI, so when a turn fails the
# error text is the only place the user can learn which model was asked for.
# Keyed by assistant_message_id; drained by on_done / on_error alongside the
# accumulator.
_turn_context: dict[uuid.UUID, dict] = {}

# How many recent messages to feed the model. Keeps context bounded without a
# summarization pass (that's a v2 refinement).
_MAX_HISTORY = 20


async def _run_web_search_for_turn(
    conversation: Conversation,
    assistant_message_id: uuid.UUID,
    user_query: str,
) -> str | None:
    """Run a web search before this turn's inference and persist the result
    as a TOOL-role message. Returns a system-message payload to inject into
    the model's prompt, or None if the search produced nothing useful
    (budget exhausted / backend down / zero results — every failure mode
    degrades to "answer from your own knowledge").

    Publishes `tool_status` events on the conversation topic so the browser
    can render a live "🌐 Searching: …" badge while the call is in flight.

    If the feature is globally off (no provider configured) we return None
    silently — no transcript row, no events. Per-conversation flags can
    linger from earlier sessions and we don't want to spam the thread.
    """
    if web_search.get_provider() is None:
        return None

    # Pre-flight: tell the UI we're searching.
    await bus.publish(
        bus.conversation_topic(conversation.id),
        {
            "type": "tool_status",
            "assistant_message_id": str(assistant_message_id),
            "phase": "searching",
            "query": user_query,
        },
    )

    response = await web_search.search(
        user_query, site=getattr(conversation, "search_site", None)
    )

    # Persist a TOOL row regardless of outcome — the audit trail should show
    # that we tried even when the result was empty / errored.
    tool_payload = {
        "query": user_query,
        "results": [r.model_dump() for r in response.results] if response else [],
        "elapsed_ms": response.elapsed_ms if response else None,
        "backend": response.backend if response else None,
        "ok": response is not None,
    }
    async with SessionLocal() as session:
        async with session.begin():
            session.add(ConversationMessage(
                conversation_id=conversation.id,
                role=MessageRole.TOOL,
                content="",  # the structured data lives in tool_payload
                complete=True,
                tool_name="web_search",
                tool_payload=tool_payload,
            ))

    # Post-flight: signal completion so the badge collapses into a summary.
    await bus.publish(
        bus.conversation_topic(conversation.id),
        {
            "type": "tool_status",
            "assistant_message_id": str(assistant_message_id),
            "phase": "done",
            "query": user_query,
            "result_count": len(response.results) if response else 0,
            "ok": response is not None,
        },
    )

    if response is None:
        return None
    return web_search.format_results_for_prompt(response)


async def handle_tool_call(
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
    tool_call_id: str,
    name: str,
    arguments: dict,
) -> dict:
    """Run a model-requested tool (agentic mode), persist a TOOL transcript
    row, publish tool_status events for the live badge, and return the result
    dict the WS handler ships back as ChatToolResultMsg.

    Imported lazily-ish via the tools module to avoid a chat→tools→chat
    import cycle (tools imports web_search/web_fetch only)."""
    from app.orchestrator.tools import run_tool

    # Enforce a conversation-level site restriction on searches. The hard
    # limit wins over whatever the model passed (or didn't) in `site`.
    if name == "web_search":
        async with SessionLocal() as session:
            convo = await session.get(Conversation, conversation_id)
        convo_site = getattr(convo, "search_site", None) if convo else None
        if convo_site:
            arguments = {**arguments, "site": convo_site}

    # Phase: started — drives the "🔧 web_search: …" badge.
    await bus.publish(
        bus.conversation_topic(conversation_id),
        {
            "type": "tool_status",
            "assistant_message_id": str(assistant_message_id),
            "phase": "tool_start",
            "tool": name,
            "arguments": arguments,
        },
    )

    text, payload = await run_tool(name, arguments, conversation_id=conversation_id)

    # Persist a TOOL transcript row so the conversation shows what ran.
    async with SessionLocal() as session:
        async with session.begin():
            session.add(ConversationMessage(
                conversation_id=conversation_id,
                role=MessageRole.TOOL,
                content="",
                complete=True,
                tool_name=name,
                tool_payload={"arguments": arguments, **payload},
            ))

    await bus.publish(
        bus.conversation_topic(conversation_id),
        {
            "type": "tool_status",
            "assistant_message_id": str(assistant_message_id),
            "phase": "tool_done",
            "tool": name,
            "arguments": arguments,
            "payload": payload,
        },
    )

    return {"success": True, "content": text}


def web_enabled(conversation: Conversation) -> bool:
    """Read the conversation's web INTENT, tolerating pre-Pilot values.

    The boot migration rewrites "search"/"tools" to "on", but a row written by
    an older process mid-deploy would still carry them.
    """
    raw = (getattr(conversation, "web_mode", None) or "").strip().lower()
    if raw in ("on", "search", "tools"):
        return True
    if raw == "off":
        return False
    return bool(conversation.web_search_enabled)


async def _no_worker_error(config: pilot.ModeConfig) -> str:
    """Explain an empty pool in mode language, and point at the way out.

    The user picked "Thoughtful", not a pool, so naming only the pool would be
    a non-sequitur. Check the other mode too — most of the time one machine is
    simply asleep and switching modes is the fastest fix.
    """
    msg = f"No worker is online for {config.label} mode (pool '{config.pool}')."
    other = pilot.DEEP if config.mode == pilot.LIGHT else pilot.LIGHT
    other_config = await pilot.get_mode_config(other)
    if other_config.pool != config.pool and await registry.by_pool(other_config.pool):
        return (
            f"{msg} {other_config.label} mode has a worker online — switch this "
            f"thread to it above, or repoint the mode in Pilot settings."
        )
    return (
        f"{msg} Start a worker in that pool, or point the mode at a different "
        f"one in Pilot settings."
    )


async def dispatch_turn(
    conversation: Conversation,
    assistant_message_id: uuid.UUID,
    history: list[dict[str, str]],
) -> str | None:
    """Send a chat turn to a worker. Returns None on success, or an error
    string the caller should record on the assistant message.

    The conversation's mode is resolved to a concrete (pool, model) here, on
    every turn. Changing a mode's settings therefore takes effect on existing
    threads — including threads whose last turn failed.
    """
    config = await pilot.get_mode_config(conversation.mode)
    workers = await registry.by_pool(config.pool)
    if not workers:
        return await _no_worker_error(config)

    # Prefer a fully idle worker; fall back to any so a busy fleet still
    # answers (Ollama will just serialize the inference).
    idle = [w for w in workers if w.current_task_id is None]
    worker = (idle or workers)[0]

    # Mechanism is derived, never stored: the conversation holds only on/off.
    # A tools-enabled mode whose model can't call tools degrades to "search"
    # rather than sending a `tools` field Ollama will silently drop.
    web_mode = pilot.web_mechanism(config, web_enabled(conversation))

    # ── "search" mode (Phase 1: pre-flight injection) ───────────────
    # Run a search against the latest user message and prepend a synthetic
    # system message with the results. Works on any model; no tool support
    # needed.
    if web_mode == "search":
        latest_user = next(
            (m["content"] for m in reversed(history)
             if m.get("role") == "user" and m.get("content")),
            None,
        )
        if latest_user:
            injection = await _run_web_search_for_turn(
                conversation, assistant_message_id, latest_user.strip()
            )
            if injection:
                # Slip the system message in just BEFORE the latest user turn
                # so the model treats it as relevant context for that
                # question, not generic guidance.
                insert_at = len(history) - 1
                # Walk back past trailing assistant rows (defensive — shouldn't
                # happen because we just added a user message, but cheap).
                while insert_at > 0 and history[insert_at].get("role") != "user":
                    insert_at -= 1
                history = (
                    history[:insert_at]
                    + [{"role": "system", "content": injection}]
                    + history[insert_at:]
                )

    # Trim history to the most recent turns; always keep a leading system
    # message if there is one.
    trimmed = history
    if len(history) > _MAX_HISTORY:
        head = [m for m in history[:1] if m.get("role") == "system"]
        trimmed = head + history[-_MAX_HISTORY:]

    # ── "tools" mode (Phase 2: agentic loop) ────────────────────────
    # Hand the model the tool schemas. The worker runs the search→fetch loop
    # and emits ChatToolCallMsg for each call; routes/workers.py runs the
    # tool and replies. Only effective on tool-capable models; others simply
    # ignore the `tools` field and answer directly.
    # Built per-turn rather than a module constant: generate_image is only
    # offered when an imager is online and idle. See tools.available_tools.
    tools = await available_tools() if web_mode == "tools" else None
    if tools:
        # Prepend tool-use guidance so the model composes thoughtful queries
        # and chains search→fetch, rather than doing one verbatim lookup.
        # Goes ahead of any user-set system prompt without replacing it.
        trimmed = [{"role": "system", "content": TOOLS_GUIDANCE}] + trimmed

    msg = ChatRequestMsg(
        conversation_id=conversation.id,
        assistant_message_id=assistant_message_id,
        # None means "use that worker's own DEFAULT_MODEL", which is the one
        # model a worker can never 404 on.
        model=config.model,
        messages=trimmed,
        tools=tools,
    )
    try:
        await worker.send(msg.model_dump(mode="json"))
    except Exception as exc:
        log.warning("chat.dispatch_send_failed", worker=worker.name, error=str(exc))
        return f"Failed to reach worker {worker.name}: {exc}"

    _accumulators[assistant_message_id] = ""
    _turn_context[assistant_message_id] = {
        "worker": worker.name,
        "model": config.model,
        "label": config.label,
    }
    # Record which worker and model are handling this turn so the UI can show
    # it and a failure is diagnosable.
    async with SessionLocal() as session:
        async with session.begin():
            am = await session.get(ConversationMessage, assistant_message_id)
            if am:
                am.worker_name = worker.name
                am.model_used = config.model
    log.info(
        "chat.dispatched",
        conversation=str(conversation.id),
        mode=config.mode,
        pool=config.pool,
        worker=worker.name,
        model=config.model,
        web=web_mode,
    )
    return None


async def on_chunk(conversation_id: uuid.UUID, assistant_message_id: uuid.UUID, delta: str) -> None:
    """Accumulate a streamed delta and republish the running full text."""
    running = _accumulators.get(assistant_message_id, "") + delta
    _accumulators[assistant_message_id] = running
    await bus.publish(
        bus.conversation_topic(conversation_id),
        {
            "type": "chunk",
            "assistant_message_id": str(assistant_message_id),
            "content": running,
        },
    )


async def on_done(
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
    content: str,
) -> None:
    """Persist the final assistant content and signal the SSE stream."""
    _accumulators.pop(assistant_message_id, None)
    _turn_context.pop(assistant_message_id, None)
    async with SessionLocal() as session:
        async with session.begin():
            am = await session.get(ConversationMessage, assistant_message_id)
            if am:
                am.content = content
                am.complete = True
                am.error = None
                # Bump created_at to completion time so the assistant reply
                # sorts AFTER any TOOL rows persisted mid-turn (agentic mode).
                # Without this the reply would render above the tool cards
                # that informed it, since the empty row predates them.
                am.created_at = datetime.now(timezone.utc)
    await bus.publish(
        bus.conversation_topic(conversation_id),
        {
            "type": "done",
            "assistant_message_id": str(assistant_message_id),
            "content": content,
        },
    )
    log.info("chat.turn_done", conversation=str(conversation_id))


def _humanize_error(error: str, context: dict) -> str:
    """Translate a raw worker/httpx error into something actionable.

    Worth the trouble because Pilot hides the model name: an unadorned
    "Client error '404 Not Found' for url http://host.docker.internal:11434
    /api/chat" tells the user nothing they can act on, and 404 from Ollama's
    chat endpoint means one specific thing — the requested model tag isn't
    pulled on that machine.
    """
    worker = context.get("worker") or "the worker that answered"
    model = context.get("model")
    lowered = error.lower()

    if "404" in error and "/api/chat" in lowered:
        named = f"'{model}'" if model else "That worker's default model"
        return (
            f"{named} isn't installed on {worker} — its Ollama returned 404. "
            f"Pick a model that worker actually has in Pilot settings, or "
            f"pull it on that machine."
        )
    if any(x in lowered for x in (
        "connection refused", "all connection attempts failed", "connecterror",
    )):
        return (
            f"{worker} couldn't reach its Ollama host. Check that Ollama is "
            f"running on that machine."
        )
    if "timeout" in lowered or "timed out" in lowered:
        return f"{worker} didn't finish in time. Try a shorter prompt."
    return error


async def on_error(
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
    error: str,
) -> None:
    """Record a failed turn on the assistant message and signal the stream."""
    partial = _accumulators.pop(assistant_message_id, "")
    context = _turn_context.pop(assistant_message_id, {})
    error = _humanize_error(error, context)
    async with SessionLocal() as session:
        async with session.begin():
            am = await session.get(ConversationMessage, assistant_message_id)
            if am:
                am.content = partial
                am.complete = True
                am.error = error
                am.created_at = datetime.now(timezone.utc)
    # Named "turn_error" rather than "error" so it doesn't collide with the
    # browser EventSource's built-in connection-error event.
    await bus.publish(
        bus.conversation_topic(conversation_id),
        {
            "type": "turn_error",
            "assistant_message_id": str(assistant_message_id),
            "error": error,
        },
    )
    log.warning("chat.turn_error", conversation=str(conversation_id), error=error)
