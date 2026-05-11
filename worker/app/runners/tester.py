"""Tester runner — write or extend tests for a target.

MVP behavior: emit a unified diff containing new/modified test files. Later
iterations will actually execute the test suite inside the worker container
and stream pass/fail back as a test_report artifact.
"""
from __future__ import annotations

from app.git_workspace import GitWorkspace
from app.runners.base import BaseRunner, RunnerResult, extract_diff


_TESTER_INSTRUCTIONS = """\
You write tests. Examine the project, then output a unified diff that adds or
modifies test files only — no production code changes. Follow the project's
existing test framework and conventions. Output a single ```diff fenced block.
"""


class TesterRunner(BaseRunner):
    role = "tester"

    def system_prompt(self) -> str:
        return super().system_prompt() + "\n\n" + _TESTER_INSTRUCTIONS

    async def setup(self) -> None:
        ws = GitWorkspace(
            self.ctx.project["github_owner"],
            self.ctx.project["github_repo"],
            self.ctx.project.get("default_branch", "main"),
        )
        self._ws = ws
        await ws.__aenter__()
        files = ws.list_files()
        tests = [f for f in files if "test" in f.lower()][:80]
        self._tests_summary = "\n".join(tests) if tests else "(no existing tests found)"
        await self.ctx.emit_log("system", f"existing test files: {len(tests)}")

    def user_prompt(self) -> str:
        return (
            f"Existing test files (truncated):\n{self._tests_summary}\n\n"
            f"## Task\n{self.ctx.prompt}\n"
        )

    async def finalize(self, response_text: str) -> RunnerResult:
        try:
            patch = extract_diff(response_text)
            if not patch:
                await self.ctx.emit_artifact("text", "raw_response.txt", response_text, {})
                return RunnerResult(success=False, summary="tester: no diff produced")
            await self.ctx.emit_artifact("patch", "tests.diff", patch, {"role": "tester"})
            return RunnerResult(success=True, summary="tester produced a test diff")
        finally:
            await self._ws.__aexit__(None, None, None)
