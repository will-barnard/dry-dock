"""Tester runner — write or extend tests via SEARCH/REPLACE blocks.

MVP behavior: emit edits that touch test files only. Later iterations will
actually execute the test suite inside the worker container and stream
pass/fail back as a structured test_report artifact.
"""
from __future__ import annotations

from app.git_workspace import GitWorkspace
from app.runners.base import (
    ApplyError,
    BaseRunner,
    RunnerResult,
    SEARCH_REPLACE_INSTRUCTIONS,
    apply_search_replace_blocks,
    extract_diff,
    extract_search_replace_blocks,
    relevant_files_for_prompt,
    render_file_contents,
)


_TESTER_INSTRUCTIONS = """\
You write tests. Examine the project, then emit SEARCH/REPLACE blocks that add
or modify test files only — no production code changes. Follow the project's
existing test framework and conventions.
""" + "\n" + SEARCH_REPLACE_INSTRUCTIONS


class TesterRunner(BaseRunner):
    role = "tester"

    def system_prompt(self) -> str:
        return super().system_prompt() + "\n\n" + _TESTER_INSTRUCTIONS

    async def setup(self) -> None:
        branch = self.ctx.branch_name or self.ctx.project.get("default_branch", "main")
        ws = GitWorkspace(
            self.ctx.project["github_owner"],
            self.ctx.project["github_repo"],
            branch,
        )
        self._ws = ws
        await ws.__aenter__()

        all_files = [
            f for f in ws.list_files()
            if not f.startswith((".git/", "node_modules/", ".venv/", "dist/", "build/"))
        ]
        # Existing test files are the most useful context for a tester.
        test_files = [f for f in all_files if "test" in f.lower()][:80]
        prompt_files = relevant_files_for_prompt(self.ctx.prompt, all_files, max_files=6)
        # Merge prompt-mentioned + first few existing tests so the model sees both
        # the conventions in use and the production code it's testing.
        selected = list(dict.fromkeys(prompt_files + test_files[:4]))
        self._file_section = render_file_contents(self._ws, selected)
        self._tests_summary = "\n".join(test_files) or "(no existing tests found)"
        await self.ctx.emit_log(
            "system", f"branch={branch}, existing test files: {len(test_files)}",
        )

    def user_prompt(self) -> str:
        return (
            f"## Existing test files (truncated)\n{self._tests_summary}\n\n"
            f"## Current contents of likely target files\n"
            f"{self._file_section or '(nothing matched by name)'}\n\n"
            f"## Task\n{self.ctx.prompt}\n"
        )

    async def finalize(self, response_text: str) -> RunnerResult:
        try:
            await self.ctx.emit_artifact("text", "raw_response.txt", response_text, {})

            blocks = extract_search_replace_blocks(response_text)
            if blocks:
                try:
                    modified = apply_search_replace_blocks(self._ws, blocks)
                except ApplyError as exc:
                    await self.ctx.emit_log("stderr", f"SR apply failed: {exc}")
                    return RunnerResult(success=False, summary=f"tester: {exc}")

                diff = await self._ws.diff()
                if not diff.strip():
                    return RunnerResult(
                        success=False, summary="tester: SR blocks produced no net change"
                    )

                await self.ctx.emit_artifact(
                    "patch", "tests.diff", diff,
                    {"role": "tester", "format": "search-replace", "files": modified},
                )
                return RunnerResult(
                    success=True, summary=f"added/modified {len(modified)} test file(s)",
                    payload={"files": modified, "format": "search-replace"},
                )

            patch = extract_diff(response_text)
            if patch:
                await self.ctx.emit_artifact(
                    "patch", "tests.diff", patch,
                    {"role": "tester", "format": "unified-diff"},
                )
                return RunnerResult(success=True, summary="tester produced a test diff")

            return RunnerResult(
                success=False, summary="tester: no SEARCH/REPLACE blocks or diff in output"
            )
        finally:
            await self._ws.__aexit__(None, None, None)
