"""Reviewer runner — read a branch/diff and produce a code-review markdown report."""
from __future__ import annotations

from app.git_workspace import GitWorkspace
from app.runners.base import BaseRunner, RunnerResult


_REVIEWER_INSTRUCTIONS = """\
You are reviewing a code change. Output a markdown report with these sections:
  ## Summary
  ## Issues  (one bullet per issue, severity tag in brackets: [blocker], [major], [minor], [nit])
  ## Suggestions
  ## Approval recommendation  (approve | request_changes | comment)

Be concise. Skip sections you have nothing for. No flattery.
"""


class ReviewerRunner(BaseRunner):
    role = "reviewer"

    def system_prompt(self) -> str:
        return super().system_prompt() + "\n\n" + _REVIEWER_INSTRUCTIONS

    async def setup(self) -> None:
        # Fetch the diff that the payload tells us to review.
        # For MVP we trust the payload to contain a 'diff' string; later we'll
        # fetch from the orchestrator by run/artifact id.
        self._diff = (self.ctx.payload or {}).get("diff", "")
        if not self._diff:
            await self.ctx.emit_log("stderr", "reviewer: no diff in payload")

    def user_prompt(self) -> str:
        return f"## Diff to review\n\n```diff\n{self._diff}\n```\n\n## Reviewer brief\n{self.ctx.prompt}"

    async def finalize(self, response_text: str) -> RunnerResult:
        await self.ctx.emit_artifact("review", "review.md", response_text, {})
        verdict = "comment"
        lower = response_text.lower()
        if "request_changes" in lower or "request changes" in lower:
            verdict = "request_changes"
        elif "approve" in lower.split("approval recommendation", 1)[-1][:200]:
            verdict = "approve"
        return RunnerResult(success=True, summary=f"review: {verdict}", payload={"verdict": verdict})
