"""Task → worker routing.

Given a task that needs a particular pool, pick the best live worker from that
pool given declared capabilities (RAM, max context, installed model). For MVP
this is a simple filter+sort; later this is where we'd plug in priority queues,
backpressure, or cross-pool fallback (e.g. coder pool spilling to general).
"""
from __future__ import annotations

from app.models import Task
from app.orchestrator.registry import LiveWorker, registry


def _worker_can_run(worker: LiveWorker, task: Task) -> bool:
    if worker.current_task_id is not None:
        return False
    if task.min_ram_gb and worker.ram_gb < task.min_ram_gb:
        return False
    if task.min_context and worker.max_context < task.min_context:
        return False
    if task.preferred_model and task.preferred_model not in worker.installed_models:
        return False
    return True


async def select_worker_for_task(task: Task) -> LiveWorker | None:
    candidates = await registry.by_pool(task.required_pool)
    candidates = [w for w in candidates if _worker_can_run(w, task)]
    if not candidates:
        return None
    # Prefer workers with the largest declared context (proxy for capability) and
    # most RAM headroom. Tie-break is arbitrary.
    candidates.sort(key=lambda w: (w.max_context, w.ram_gb), reverse=True)
    return candidates[0]
