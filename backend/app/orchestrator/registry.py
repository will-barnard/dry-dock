"""Live worker registry — tracks open WebSocket connections in-process.

A worker = one persistent WS connection. The registry knows which workers
are connected, what pool they're in, and how to push messages to them.
DB state (`workers` table) is the durable record; the registry is the
ephemeral runtime view.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket


@dataclass
class LiveWorker:
    worker_id: uuid.UUID
    name: str
    pool: str
    socket: WebSocket
    installed_models: list[str] = field(default_factory=list)
    max_context: int = 8192
    ram_gb: int = 0
    current_task_id: uuid.UUID | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, payload: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.socket.send_json(payload)


class WorkerRegistry:
    def __init__(self) -> None:
        self._workers: dict[uuid.UUID, LiveWorker] = {}
        self._lock = asyncio.Lock()

    async def add(self, worker: LiveWorker) -> None:
        async with self._lock:
            self._workers[worker.worker_id] = worker

    async def remove(self, worker_id: uuid.UUID) -> None:
        async with self._lock:
            self._workers.pop(worker_id, None)

    async def get(self, worker_id: uuid.UUID) -> LiveWorker | None:
        async with self._lock:
            return self._workers.get(worker_id)

    async def by_pool(self, pool: str) -> list[LiveWorker]:
        async with self._lock:
            return [w for w in self._workers.values() if w.pool == pool]

    async def all(self) -> list[LiveWorker]:
        async with self._lock:
            return list(self._workers.values())


registry = WorkerRegistry()
