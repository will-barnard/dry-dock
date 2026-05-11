"""Planner runner — emit a JSON plan of child tasks.

The planner is given a high-level goal and must produce a JSON array of task
specs. Each entry is {kind, title, prompt, required_pool, depends_on?, preferred_model?}.
The orchestrator materializes these as Task rows once approved.
"""
from __future__ import annotations

import json
import re

from app.runners.base import BaseRunner, RunnerResult, extract_fenced_blocks


_PLANNER_INSTRUCTIONS = """\
Your job is to break a software engineering goal into an ordered list of agent tasks.

Allowed task kinds and the pool each runs on:
  - code       (coder)         — write or modify source code
  - review     (reviewer)      — review a diff or branch
  - test       (tester)        — write or run tests
  - refactor   (refactorer)    — restructure existing code without changing behavior
  - docs       (docs)          — write or update documentation
  - research   (researcher)    — gather information and summarize

Output ONLY a single JSON code block. The JSON is an array of task objects with:
  - kind: one of the allowed kinds above
  - title: short title (<60 chars)
  - prompt: full instructions the executing agent should follow
  - required_pool: typically the matching pool name above
  - depends_on (optional): integer index of an earlier task this depends on
  - preferred_model (optional): e.g. "qwen2.5-coder:32b"

Keep the plan small (3–8 tasks). Don't pad. No prose outside the JSON block.
"""


class PlannerRunner(BaseRunner):
    role = "planner"

    def system_prompt(self) -> str:
        return super().system_prompt() + "\n\n" + _PLANNER_INSTRUCTIONS

    async def finalize(self, response_text: str) -> RunnerResult:
        plan_json = self._extract_plan(response_text)
        if plan_json is None:
            await self.ctx.emit_log("stderr", "planner produced no valid JSON block")
            return RunnerResult(
                success=False,
                summary="planner: could not parse JSON plan from response",
            )

        # Persist the plan as a 'summary' artifact — the orchestrator's
        # materialize_plan() reads from this slot.
        await self.ctx.emit_artifact("summary", "plan.json", json.dumps(plan_json, indent=2), {})
        await self.ctx.emit_artifact("text", "raw_response.txt", response_text, {})
        return RunnerResult(
            success=True,
            summary=f"plan with {len(plan_json)} step(s)",
            payload={"plan_size": len(plan_json)},
        )

    @staticmethod
    def _extract_plan(text: str) -> list[dict] | None:
        for tag, body in extract_fenced_blocks(text):
            if tag.lower() in {"json", ""}:
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, list):
                        return parsed
                except json.JSONDecodeError:
                    continue
        # Last resort: try to find a JSON array anywhere.
        m = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                return None
        return None
