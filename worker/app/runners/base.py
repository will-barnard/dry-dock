"""Base class for role-specific runners.

A runner gets a RunnerContext (task fields + a callback for log/artifact
emission) and returns a RunnerResult. Subclasses override system_prompt(),
build_messages(), and optionally produce_artifacts().
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.config import get_settings
from app.git_workspace import GitWorkspace
from app.ollama_client import get_provider

log = structlog.get_logger()


@dataclass
class RunnerContext:
    task_id: str
    run_id: str
    title: str
    prompt: str
    project: dict[str, Any]
    payload: dict[str, Any]
    preferred_model: str | None
    emit_log: Callable[[str, str], Awaitable[None]]  # (stream, body)
    emit_artifact: Callable[[str, str, str, dict], Awaitable[None]]  # (kind, name, content, metadata)


@dataclass
class RunnerResult:
    success: bool
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0


class BaseRunner:
    role: str = "base"

    def __init__(self, ctx: RunnerContext):
        self.ctx = ctx
        self.provider = get_provider()
        self.model = ctx.preferred_model or get_settings().default_model

    # ── Subclass hooks ──────────────────────────────────────────────

    def system_prompt(self) -> str:
        project_prompt = self.ctx.project.get("system_prompt") or ""
        return (
            f"You are a {self.role} agent in the dry-dock multi-agent platform. "
            f"Be concise, deliberate, and produce exactly the output format requested.\n\n"
            f"{project_prompt}"
        )

    def user_prompt(self) -> str:
        return self.ctx.prompt

    async def setup(self) -> None:
        """Called before the LLM is invoked. Subclasses may clone the repo here."""
        return

    async def finalize(self, response_text: str) -> RunnerResult:
        """Called after the LLM returns. Subclasses parse the response and emit
        their domain-specific artifacts here."""
        await self.ctx.emit_artifact("text", "response.txt", response_text, {})
        return RunnerResult(success=True, summary=response_text[:300])

    # ── Driver ──────────────────────────────────────────────────────

    async def run(self) -> RunnerResult:
        await self.setup()
        messages = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": self.user_prompt()},
        ]
        await self.ctx.emit_log("system", f"model={self.model} role={self.role}")

        chunks: list[str] = []
        tokens_in = 0
        tokens_out = 0
        try:
            async for ev in self.provider.chat_stream(self.model, messages):
                msg = ev.get("message") or {}
                piece = msg.get("content") or ""
                if piece:
                    chunks.append(piece)
                    await self.ctx.emit_log("stdout", piece)
                if ev.get("done"):
                    tokens_in = ev.get("prompt_eval_count", 0) or 0
                    tokens_out = ev.get("eval_count", 0) or 0
        except Exception as exc:
            log.exception("runner.chat_failed")
            await self.ctx.emit_log("stderr", f"chat failed: {exc}")
            return RunnerResult(success=False, summary=f"chat failed: {exc}")

        response = "".join(chunks)
        result = await self.finalize(response)
        result.tokens_in = tokens_in
        result.tokens_out = tokens_out
        return result


# ── Shared helpers ──────────────────────────────────────────────────


_FENCE_RE = re.compile(r"```([a-zA-Z0-9_\-+.]*)\n(.*?)```", re.DOTALL)


def extract_fenced_blocks(text: str) -> list[tuple[str, str]]:
    """Return [(language_tag, body), ...] for every fenced code block."""
    return [(m.group(1), m.group(2)) for m in _FENCE_RE.finditer(text)]


def extract_diff(text: str) -> str | None:
    """Pull the first ```diff``` (or unmarked ```...```) block that looks like a unified diff."""
    for tag, body in extract_fenced_blocks(text):
        if tag.lower() in {"diff", "patch"}:
            return body
        if body.lstrip().startswith(("diff --git", "--- ", "Index: ")):
            return body
    return None


async def with_workspace(project: dict[str, Any]):
    """Context manager wrapping GitWorkspace."""
    return GitWorkspace(
        github_owner=project["github_owner"],
        github_repo=project["github_repo"],
        default_branch=project.get("default_branch", "main"),
    )
