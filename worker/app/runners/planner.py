"""Planner runner — emit a contract + ordered task list as a single JSON object.

The plan task is the start of every multi-step workflow. Its job is to produce
two artifacts:

1. A **contract** — a markdown spec of the shared invariants downstream agents
   must agree on: API routes, env vars, service URLs, ports, anything where
   a coder + tester drifting apart would produce broken work.

2. A **task list** — small, atomic tasks (≤3 files each, single observable
   outcome) with explicit `target_files`. The orchestrator materializes these
   as Task rows on approval, copying the contract + target_files into each
   child's payload so downstream runners can use them without lookup.

Both live inside one JSON object so a single artifact carries everything
materialize_plan needs.
"""
from __future__ import annotations

import json
import re

import structlog

from app.git_workspace import GitWorkspace
from app.runners.base import BaseRunner, RunnerResult, extract_fenced_blocks

log = structlog.get_logger()


_PLANNER_INSTRUCTIONS = """\
Your job is to plan a software engineering goal. You output two things wrapped
in a single JSON code block:

1. A **contract** — markdown describing the shared invariants every downstream
   agent must respect. Include (when relevant to the goal):
     - API endpoints (METHOD path → request/response shape)
     - Environment variables (name, purpose, example value)
     - Service-to-service URLs and ports
     - Database tables / schema additions
     - Any cross-cutting concerns (auth model, routing rules)
   Be specific. "Backend exposes /api/animals" is not enough — write
   "POST /api/animals body {name, species} → 201 {id, name, species}".

2. A **task list** — small, atomic units of work.

Allowed task kinds and the pool each runs on:
  - code       (coder)         — write or modify source code
  - review     (reviewer)      — review a diff or branch
  - test       (tester)        — write or run tests
  - refactor   (refactorer)    — restructure existing code without changing behavior
  - docs       (docs)          — write or update documentation
  - research   (researcher)    — gather information and summarize
  - validate   (validator)     — run the project's shell-level check commands
                                  (build / lint / typecheck / test). No LLM
                                  inference. Use ONE after each code/refactor
                                  task to gate progress on the integration
                                  surface compiling cleanly. Validator tasks
                                  need no target_files and a one-line prompt;
                                  the runner uses the project's configured
                                  validate_commands automatically.

Output FORMAT — exactly one ```json fenced code block containing an object:

```json
{
  "contract": "## API\\n- POST /api/animals → ...\\n## Env\\n- DATABASE_URL ...\\n## Ports\\n- backend:3001 ...",
  "tasks": [
    {
      "kind": "code",
      "title": "short title",
      "prompt": "full instructions for the executing agent — include enough context that the agent doesn't have to guess",
      "target_files": ["src/api/animals.py", "src/models/animal.py"],
      "required_pool": "coder",
      "depends_on": null,
      "preferred_model": null
    }
  ]
}
```

Rules — read carefully, these are non-negotiable:

- BREAK WORK SMALL. Each `code` task should modify at most 3 files and have a
  single observable outcome (one endpoint, one component, one migration).
  Prefer 8 small tasks over 2 huge ones. Coders hallucinate when overloaded.
- INSERT a `validate` task with depends_on pointing at each significant code or
  refactor task. This runs the project's build/lint/test commands and
  auto-requeues the parent if they fail. Skip validate for pure-docs work.
- target_files is REQUIRED for code/refactor/test/docs tasks. Look at the
  repo file tree below and pick real paths. For new files, write the path
  you intend to create.
- target_files is OPTIONAL for review/research tasks.
- Use `depends_on: <integer index>` to express order. A tester for endpoint X
  depends_on the coder task that built endpoint X.
- The total plan should be 5-15 tasks for a typical feature.
- Output ONLY the JSON code block. No prose before or after.
"""


class PlannerRunner(BaseRunner):
    role = "planner"

    def system_prompt(self) -> str:
        return super().system_prompt() + "\n\n" + _PLANNER_INSTRUCTIONS

    async def setup(self) -> None:
        # Give the planner the file tree so it can choose real paths for
        # target_files. Clone is shallow + ephemeral, same as other runners.
        try:
            ws = GitWorkspace(
                self.ctx.project["github_owner"],
                self.ctx.project["github_repo"],
                self.ctx.project.get("default_branch", "main"),
            )
            await ws.__aenter__()
            try:
                self._tree = "\n".join(
                    f for f in ws.list_files()
                    if not f.startswith(
                        (".git/", "node_modules/", ".venv/", "dist/", "build/")
                    )
                )
            finally:
                await ws.__aexit__(None, None, None)
        except Exception as exc:
            await self.ctx.emit_log("stderr", f"planner: could not list repo files: {exc}")
            self._tree = "(repo file tree unavailable — proceed without paths)"

    def user_prompt(self) -> str:
        return (
            f"## Repository file tree\n{self._tree}\n\n"
            f"## Goal\n{self.ctx.prompt}\n"
        )

    async def finalize(self, response_text: str) -> RunnerResult:
        parsed = self._extract_plan_object(response_text)
        if parsed is None:
            await self.ctx.emit_log("stderr", "planner produced no valid JSON object")
            await self.ctx.emit_artifact("text", "raw_response.txt", response_text, {})
            return RunnerResult(
                success=False,
                summary="planner: could not parse JSON plan from response",
            )

        contract = (parsed.get("contract") or "").strip()
        tasks = parsed.get("tasks") or []
        if not isinstance(tasks, list):
            await self.ctx.emit_log("stderr", "planner: tasks field is not a list")
            await self.ctx.emit_artifact("text", "raw_response.txt", response_text, {})
            return RunnerResult(success=False, summary="planner: tasks field is not a list")

        # The summary artifact carries everything materialize_plan needs — it
        # reads the JSON, splits contract + tasks, and copies both into each
        # child task's payload at materialization time.
        await self.ctx.emit_artifact(
            "summary", "plan.json",
            json.dumps({"contract": contract, "tasks": tasks}, indent=2),
            {},
        )
        # Also emit the contract by itself so it renders nicely in the
        # dashboard's artifact list (and is grep-able from outside).
        if contract:
            await self.ctx.emit_artifact("text", "contract.md", contract, {"role": "contract"})
        await self.ctx.emit_artifact("text", "raw_response.txt", response_text, {})

        return RunnerResult(
            success=True,
            summary=f"plan with {len(tasks)} task(s)"
                    f"{' + contract' if contract else ' (no contract)'}",
            payload={"plan_size": len(tasks), "has_contract": bool(contract)},
        )

    @staticmethod
    def _extract_plan_object(text: str) -> dict | None:
        """Find the JSON object the planner emitted. Accept either a wrapped
        object (the new format) or a bare array (legacy)."""
        # First try fenced blocks tagged json or untagged.
        for tag, body in extract_fenced_blocks(text):
            if tag.lower() not in {"json", ""}:
                continue
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                # Legacy bare-array — wrap into the new shape with no contract.
                return {"contract": "", "tasks": parsed}
        # Last resort: scan the whole text for any JSON object or array.
        for pattern in (r"\{\s*\"(?:contract|tasks)\".*\}", r"\[\s*\{.*\}\s*\]"):
            m = re.search(pattern, text, re.DOTALL)
            if not m:
                continue
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"contract": "", "tasks": parsed}
        return None
