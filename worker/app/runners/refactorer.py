"""Refactorer runner — behavior-preserving restructuring as a unified diff."""
from __future__ import annotations

from app.git_workspace import GitWorkspace
from app.runners.base import BaseRunner, RunnerResult, extract_diff


_REFACTORER_INSTRUCTIONS = """\
You restructure code WITHOUT changing observable behavior. No new features,
no removed features, no API changes. Output a single ```diff fenced block.
Before the diff, in 3-5 bullet points, state what behavioral invariants you
believe are preserved.
"""


class RefactorerRunner(BaseRunner):
    role = "refactorer"

    def system_prompt(self) -> str:
        return super().system_prompt() + "\n\n" + _REFACTORER_INSTRUCTIONS

    async def setup(self) -> None:
        ws = GitWorkspace(
            self.ctx.project["github_owner"],
            self.ctx.project["github_repo"],
            self.ctx.project.get("default_branch", "main"),
        )
        self._ws = ws
        await ws.__aenter__()
        files = [f for f in ws.list_files() if not f.startswith((".git/", "node_modules/", ".venv/"))][:200]
        self._tree = "\n".join(files)

    def user_prompt(self) -> str:
        return f"Repo file tree (truncated):\n{self._tree}\n\n## Task\n{self.ctx.prompt}"

    async def finalize(self, response_text: str) -> RunnerResult:
        try:
            patch = extract_diff(response_text)
            if not patch:
                await self.ctx.emit_artifact("text", "raw_response.txt", response_text, {})
                return RunnerResult(success=False, summary="refactorer: no diff produced")
            preface = response_text.split("```", 1)[0].strip()
            await self.ctx.emit_artifact("patch", "refactor.diff", patch, {"role": "refactorer"})
            await self.ctx.emit_artifact("text", "invariants.txt", preface, {})
            return RunnerResult(success=True, summary=preface[:300] or "refactor diff produced")
        finally:
            await self._ws.__aexit__(None, None, None)
