"""Refactorer runner — behavior-preserving edits via SEARCH/REPLACE blocks."""
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


_REFACTORER_INSTRUCTIONS = """\
You restructure code WITHOUT changing observable behavior. No new features, no
removed features, no API changes. Before the edits, in 3-5 bullet points state
what behavioral invariants you believe are preserved. Then emit SEARCH/REPLACE
blocks. Do not output a unified diff.
""" + "\n" + SEARCH_REPLACE_INSTRUCTIONS


class RefactorerRunner(BaseRunner):
    role = "refactorer"

    def system_prompt(self) -> str:
        return super().system_prompt() + "\n\n" + _REFACTORER_INSTRUCTIONS

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
        self._target_files = relevant_files_for_prompt(self.ctx.prompt, all_files, max_files=8)
        self._file_section = render_file_contents(ws, self._target_files)
        self._tree = "\n".join(all_files[:200])
        if len(all_files) > 200:
            self._tree += f"\n… and {len(all_files) - 200} more files"

    def user_prompt(self) -> str:
        return (
            f"## Repo file tree (truncated)\n{self._tree}\n\n"
            f"## Current contents of likely target files\n"
            f"{self._file_section or '(no target files matched by name)'}\n\n"
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
                    return RunnerResult(success=False, summary=f"refactorer: {exc}")

                diff = await self._ws.diff()
                if not diff.strip():
                    return RunnerResult(
                        success=False,
                        summary="refactorer: SR blocks produced no net change",
                    )

                await self.ctx.emit_artifact(
                    "patch", "refactor.diff", diff,
                    {"role": "refactorer", "format": "search-replace", "files": modified},
                )
                preface = response_text.split("<<<<<<<", 1)[0].strip()
                if preface:
                    await self.ctx.emit_artifact("text", "invariants.txt", preface, {})
                return RunnerResult(
                    success=True,
                    summary=preface[:300] or f"refactored {len(modified)} file(s)",
                    payload={"files": modified, "format": "search-replace"},
                )

            patch = extract_diff(response_text)
            if patch:
                await self.ctx.emit_artifact(
                    "patch", "refactor.diff", patch,
                    {"role": "refactorer", "format": "unified-diff"},
                )
                return RunnerResult(success=True, summary="refactor diff produced")

            return RunnerResult(
                success=False,
                summary="refactorer: model output had no SEARCH/REPLACE blocks or diff",
            )
        finally:
            await self._ws.__aexit__(None, None, None)
