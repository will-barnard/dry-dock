"""Operator chat dispatch + streaming accumulator.

Chat is deliberately NOT routed through the Task system. A conversation turn:

  1. The route persists the user message + an empty assistant message.
  2. `dispatch_turn` picks a live worker in the conversation's pool and sends
     a `chat_request` over its WebSocket.
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

import structlog

from app.db import SessionLocal
from app.models import Conversation, ConversationMessage
from app.orchestrator.event_bus import bus
from app.orchestrator.protocol import ChatRequestMsg
from app.orchestrator.registry import registry

log = structlog.get_logger()

# Running text per in-flight assistant message. Keyed by assistant_message_id.
# Populated by on_chunk, drained by on_done / on_error.
_accumulators: dict[uuid.UUID, str] = {}

# How many recent messages to feed the model. Keeps context bounded without a
# summarization pass (that's a v2 refinement).
_MAX_HISTORY = 20


async def dispatch_turn(
    conversation: Conversation,
    assistant_message_id: uuid.UUID,
    history: list[dict[str, str]],
) -> str | None:
    """Send a chat turn to a worker. Returns None on success, or an error
    string the caller should record on the assistant message."""
    workers = await registry.by_pool(conversation.pool)
    if not workers:
        return f"No worker is online in the '{conversation.pool}' pool."

    # Prefer a fully idle worker; fall back to any so a busy fleet still
    # answers (Ollama will just serialize the inference).
    idle = [w for w in workers if w.current_task_id is None]
    worker = (idle or workers)[0]

    # Trim history to the most recent turns; always keep a leading system
    # message if there is one.
    trimmed = history
    if len(history) > _MAX_HISTORY:
        head = [m for m in history[:1] if m.get("role") == "system"]
        trimmed = head + history[-_MAX_HISTORY:]

    msg = ChatRequestMsg(
        conversation_id=conversation.id,
        assistant_message_id=assistant_message_id,
        model=conversation.model,
        messages=trimmed,
    )
    try:
        await worker.send(msg.model_dump(mode="json"))
    except Exception as exc:
        log.warning("chat.dispatch_send_failed", worker=worker.name, error=str(exc))
        return f"Failed to reach worker {worker.name}: {exc}"

    _accumulators[assistant_message_id] = ""
    # Record which worker is handling this turn so the UI can show it.
    async with SessionLocal() as session:
        async with session.begin():
            am = await session.get(ConversationMessage, assistant_message_id)
            if am:
                am.worker_name = worker.name
    log.info("chat.dispatched", conversation=str(conversation.id), worker=worker.name)
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
    async with SessionLocal() as session:
        async with session.begin():
            am = await session.get(ConversationMessage, assistant_message_id)
            if am:
                am.content = content
                am.complete = True
                am.error = None
    await bus.publish(
        bus.conversation_topic(conversation_id),
        {
            "type": "done",
            "assistant_message_id": str(assistant_message_id),
            "content": content,
        },
    )
    log.info("chat.turn_done", conversation=str(conversation_id))


async def on_error(
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
    error: str,
) -> None:
    """Record a failed turn on the assistant message and signal the stream."""
    partial = _accumulators.pop(assistant_message_id, "")
    async with SessionLocal() as session:
        async with session.begin():
            am = await session.get(ConversationMessage, assistant_message_id)
            if am:
                am.content = partial
                am.complete = True
                am.error = error
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
