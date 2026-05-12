"""Task → worker routing with strict-failover priority.

Given a task that needs a particular pool, we pick from the workers in that
pool using two filters:

1. **Capability** — RAM, max context, GPU VRAM, required model. A worker
   that doesn't have the required model installed can't run the task, and
   is filtered out before priority is even considered.

2. **Priority tier** — each worker has a per-name priority integer (lower
   = preferred), set on the Settings page. The router groups capable
   workers by tier and walks tiers low → high. Within a tier we apply
   normal selection (skip busy, prefer more context/RAM).

   The failover semantic the user wants is "if my primary is online, the
   backup stays idle." So when the lowest tier has at least one *capable*
   worker — even if they're all currently busy — we DON'T fall through to
   the next tier. We wait. Only when no capable worker exists at the
   current tier (offline, wrong model, missing GPU, …) do we drop to the
   next tier of backups.
"""
from __future__ import annotations

from collections import defaultdict

from app.models import Task
from app.orchestrator.registry import LiveWorker, registry
from app.orchestrator.settings_service import (
    get_all_worker_priorities,
    get_role_model,
)


_DEFAULT_PRIORITY = 100


def _worker_compatible(
    worker: LiveWorker, task: Task, required_model: str | None
) -> bool:
    """Does this worker *theoretically* satisfy the task's capability needs?

    Ignores busy state. Used to decide whether to wait for a busy primary
    or fall through to a backup tier.
    """
    if task.min_ram_gb and worker.ram_gb < task.min_ram_gb:
        return False
    if task.min_context and worker.max_context < task.min_context:
        return False
    if task.min_vram_gb and (worker.gpu_vram_gb or 0) < task.min_vram_gb:
        return False
    if required_model and required_model not in worker.installed_models:
        return False
    return True


def _worker_can_run(
    worker: LiveWorker, task: Task, required_model: str | None
) -> bool:
    """Is this worker available to claim this task RIGHT NOW?"""
    if worker.current_task_id is not None:
        return False
    return _worker_compatible(worker, task, required_model)


async def select_worker_for_task(task: Task) -> LiveWorker | None:
    candidates = await registry.by_pool(task.required_pool)
    if not candidates:
        return None

    required_model = task.preferred_model or await get_role_model(task.required_pool)
    priorities = await get_all_worker_priorities()

    # Bucket candidates by priority tier.
    tiers: dict[int, list[LiveWorker]] = defaultdict(list)
    for w in candidates:
        tiers[priorities.get(w.name, _DEFAULT_PRIORITY)].append(w)

    # Walk tiers from lowest priority value (most preferred) up. The first
    # tier that contains a capable worker is the only tier we consider —
    # even if every capable worker in it is currently busy, we wait
    # rather than drop to a backup tier. That's the strict-failover rule.
    for tier_val in sorted(tiers.keys()):
        tier_workers = tiers[tier_val]
        if not any(_worker_compatible(w, task, required_model) for w in tier_workers):
            # No worker here can run this task at all — try the next tier.
            continue

        free = [w for w in tier_workers if _worker_can_run(w, task, required_model)]
        if not free:
            # Capable workers exist in this tier but they're all busy.
            # Wait for them — do NOT fall through to a lower tier.
            return None

        # Within the tier, prefer larger context window then more RAM as a
        # cheap proxy for "more capable". Tie-break is arbitrary but stable.
        free.sort(key=lambda w: (w.max_context, w.ram_gb), reverse=True)
        return free[0]

    return None
