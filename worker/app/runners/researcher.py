"""Researcher runner — summarize information without touching the repo.

Output is a markdown report. No code changes. Useful for 'investigate X and
report back' steps in a plan that other runners then consume via task payload.
"""
from __future__ import annotations

from app.runners.base import BaseRunner, RunnerResult


_RESEARCHER_INSTRUCTIONS = """\
You produce concise research reports. Format:
  # Topic
  ## Findings  (numbered)
  ## Open questions
  ## Recommended next steps

No code. No diffs. No fluff. Be specific.
"""


class ResearcherRunner(BaseRunner):
    role = "researcher"

    def system_prompt(self) -> str:
        return super().system_prompt() + "\n\n" + _RESEARCHER_INSTRUCTIONS

    async def finalize(self, response_text: str) -> RunnerResult:
        await self.ctx.emit_artifact("text", "report.md", response_text, {})
        return RunnerResult(success=True, summary=response_text[:300])
