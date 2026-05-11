"""Docs runner — write or update documentation as a unified diff."""
from __future__ import annotations

from app.git_workspace import GitWorkspace
from app.runners.base import BaseRunner, RunnerResult, extract_diff


_DOCS_INSTRUCTIONS = """\
You write project documentation. Match the project's existing style.
Output a single ```diff fenced block. Touch only documentation files
(README, docs/, ADRs, code comments).
"""


class DocsRunner(BaseRunner):
    role = "docs"

    def system_prompt(self) -> str:
        return super().system_prompt() + "\n\n" + _DOCS_INSTRUCTIONS

    async def setup(self) -> None:
        ws = GitWorkspace(
            self.ctx.project["github_owner"],
            self.ctx.project["github_repo"],
            self.ctx.project.get("default_branch", "main"),
        )
        self._ws = ws
        await ws.__aenter__()
        files = ws.list_files()
        docs = [f for f in files if f.lower().endswith((".md", ".rst", ".adoc")) or f.lower().startswith("docs/")][:80]
        self._docs_summary = "\n".join(docs) or "(no doc files found)"

    def user_prompt(self) -> str:
        return f"Existing docs:\n{self._docs_summary}\n\n## Task\n{self.ctx.prompt}"

    async def finalize(self, response_text: str) -> RunnerResult:
        try:
            patch = extract_diff(response_text)
            if not patch:
                await self.ctx.emit_artifact("text", "raw_response.txt", response_text, {})
                return RunnerResult(success=False, summary="docs: no diff produced")
            await self.ctx.emit_artifact("patch", "docs.diff", patch, {"role": "docs"})
            return RunnerResult(success=True, summary="docs diff produced")
        finally:
            await self._ws.__aexit__(None, None, None)
