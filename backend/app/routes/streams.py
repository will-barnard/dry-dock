"""SSE endpoints for live log/status streaming to the dashboard."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.orchestrator.event_bus import bus

router = APIRouter(prefix="/stream", tags=["stream"])


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


def _sse_named(name: str, event: dict) -> bytes:
    """SSE message with a named event type, so EventSource.addEventListener
    can dispatch on it. Used by the Operator chat stream."""
    return f"event: {name}\ndata: {json.dumps(event)}\n\n".encode()


@router.get("/tasks/{task_id}")
async def stream_task(task_id: uuid.UUID):
    async def gen():
        yield _sse({"type": "open", "task_id": str(task_id)})
        async for event in bus.subscribe(bus.task_topic(task_id)):
            yield _sse(event)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/global")
async def stream_global():
    async def gen():
        yield _sse({"type": "open"})
        async for event in bus.subscribe(bus.global_topic()):
            yield _sse(event)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/operator/{conversation_id}")
async def stream_conversation(conversation_id: uuid.UUID):
    """Operator chat stream. Emits named events — `chunk`, `done`, `error` —
    each carrying {assistant_message_id, content|error}. The thread template's
    EventSource dispatches on the event name."""
    async def gen():
        yield _sse_named("open", {"conversation_id": str(conversation_id)})
        async for event in bus.subscribe(bus.conversation_topic(conversation_id)):
            yield _sse_named(event.get("type", "message"), event)

    return StreamingResponse(gen(), media_type="text/event-stream")
