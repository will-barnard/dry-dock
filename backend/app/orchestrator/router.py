"""Task → worker routing.

Given a task that needs a particular pool, pick the best live worker from that
pool given declared capabilities (RAM, max context, installed model). For MVP
this is a simple filter+sort; later this is where we'd plug in priority queues,
backpressure, or cross-pool fallback (e.g. coder pool spilling to general).
"""
from __future__ import annotations

from app.models import Task
from app.orchestrator.registry import LiveWorker, registry
from app.orchestrator.settings_service import get_role_model


def _worker_can_run(worker: LiveWorker, task: Task, required_model: str | None) -> bool:
    if worker.current_task_id is not None:
        return False
    if task.min_ram_gb and worker.ram_gb < task.min_ram_gb:
        return False
    if task.min_context and worker.max_context < task.min_context:
        return False
    if task.min_vram_gb and (worker.gpu_vram_gb or 0) < task.min_vram_gb:
        return False
    if required_model and required_model not in worker.installed_models:
        return False
    return True


async def select_worker_for_task(task: Task) -> LiveWorker | None:
    candidates = await registry.by_pool(task.required_pool)
    # Resolve the model the worker will be asked to run: task override → role
    # setting → env default. We filter on this so we never grant a job to a
    # worker that lacks the model the runner will try to load.
    required_model = task.preferred_model or await get_role_model(task.required_pool)
    candidates = [w for w in candidates if _worker_can_run(w, task, required_model)]
    if not candidates:
        return None
    # Prefer workers with the largest declared context (proxy for capability)
    # and most RAM headroom. Tie-break is arbitrary.
    candidates.sort(key=lambda w: (w.max_context, w.ram_gb), reverse=True)
    return candidates[0]
