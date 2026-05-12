"""Single source of truth for the kind→pool mapping and the list of pools."""
from __future__ import annotations

from app.models import TaskKind

KIND_TO_POOL: dict[TaskKind, str] = {
    TaskKind.PLAN: "planner",
    TaskKind.CODE: "coder",
    TaskKind.REVIEW: "reviewer",
    TaskKind.TEST: "tester",
    TaskKind.REFACTOR: "refactorer",
    TaskKind.DOCS: "docs",
    TaskKind.RESEARCH: "researcher",
    TaskKind.VALIDATE: "validator",
}

KNOWN_POOLS: tuple[str, ...] = (
    "planner",
    "coder",
    "reviewer",
    "tester",
    "refactorer",
    "docs",
    "researcher",
    "validator",
)


def pool_for_kind(kind: TaskKind | str) -> str:
    """Return the canonical pool for a task kind. Accepts enum or its value."""
    if isinstance(kind, str):
        kind = TaskKind(kind)
    return KIND_TO_POOL[kind]
