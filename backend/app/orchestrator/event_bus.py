"""In-process pub/sub bus for streaming events to dashboard subscribers.

Each task gets a topic; SSE handlers subscribe via `subscribe(task_id)` and
receive log/status/artifact events as they're persisted. This avoids polling
the events table for live views.

For a single-replica orchestrator this is enough. When we scale to multiple
replicas we'll replace the in-process bus with Postgres LISTEN/NOTIFY or
Redis pub/sub — the consumer-side interface stays the same.
"""
from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(self, topic: str, event: dict[str, Any]) -> None:
        async with self._lock:
            queues = list(self._subscribers.get(topic, ()))
        for q in queues:
            # Drop oldest if a subscriber falls behind so we never block the producer.
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            await q.put(event)

    async def subscribe(self, topic: str) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        async with self._lock:
            self._subscribers[topic].add(q)
        try:
            while True:
                event = await q.get()
                yield event
        finally:
            async with self._lock:
                self._subscribers[topic].discard(q)
                if not self._subscribers[topic]:
                    del self._subscribers[topic]

    @staticmethod
    def task_topic(task_id: uuid.UUID) -> str:
        return f"task:{task_id}"

    @staticmethod
    def project_topic(project_id: uuid.UUID) -> str:
        return f"project:{project_id}"

    @staticmethod
    def global_topic() -> str:
        return "global"


bus = EventBus()
