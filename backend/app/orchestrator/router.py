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


def _model_available(installed: list[str], required: str) -> bool:
    """Return True if `required` is satisfied by any installed model.

    Ollama returns full quantization tags (e.g. qwen2.5-coder:32b-instruct-q4_K_M)
    while the required model is typically the short form (qwen2.5-coder:32b).
    Accept a match if any installed model name starts with required (case-insensitive),
    which covers both exact matches and quantization-suffixed variants.
    """
    req = required.lower()
    return any(m.lower() == req or m.lower().startswith(req + "-") for m in installed)


def _worker_can_run(worker: LiveWorker, task: Task, required_model: str | None) -> bool:
    if worker.current_task_id is not None:
        return False
    if task.min_ram_gb and worker.ram_gb < task.min_ram_gb:
        return False
    if task.min_context and worker.max_context < task.min_context:
        return False
    if task.min_vram_gb and (worker.gpu_vram_gb or 0) < task.min_vram_gb:
        return False
    if required_model and not _model_available(worker.installed_models, required_model):
        return False
    return True


async def select_worker_for_task(task: Task) -> LiveWorker | None:
    candidates = await registry.by_pool(task.required_pool)
    # Only enforce model availability when the task explicitly requests a
    # specific model. If preferred_model is unset, any worker in the pool can
    # claim the job and will run it with its own DEFAULT_MODEL.
    required_model = task.preferred_model or None
    # Check role setting only if no per-task model is set AND the role has a
    # non-default override saved in app_settings (explicit admin choice).
    if not required_model:
        role_model = await get_role_model(task.required_pool)
        # Only gate on the role model if at least one pool worker actually has
        # it installed — avoids blocking dispatch when the setting is the
        # backend env default and the workers use a different local model.
        pool_workers = await registry.by_pool(task.required_pool)
        if role_model and any(_model_available(w.installed_models, role_model) for w in pool_workers):
            required_model = role_model
    candidates = [w for w in candidates if _worker_can_run(w, task, required_model)]
    if not candidates:
        return None
    # Prefer workers with the largest declared context (proxy for capability)
    # and most RAM headroom. Tie-break is arbitrary.
    candidates.sort(key=lambda w: (w.max_context, w.ram_gb), reverse=True)
    return candidates[0]
