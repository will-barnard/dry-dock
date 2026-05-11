"""Docs runner — write or update documentation via SEARCH/REPLACE blocks."""
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


_DOCS_INSTRUCTIONS = """\
You write project documentation. Match the project's existing style. Touch
only documentation files (README, docs/, ADRs, code comments). Emit
SEARCH/REPLACE blocks.
""" + "\n" + SEARCH_REPLACE_INSTRUCTIONS


class DocsRunner(BaseRunner):
    role = "docs"

    def system_prompt(self) -> str:
        return super().system_prompt() + "\n\n" + _DOCS_INSTRUCTIONS

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
        docs = [
            f for f in all_files
            if f.lower().endswith((".md", ".rst", ".adoc")) or f.lower().startswith("docs/")
        ][:80]
        prompt_files = relevant_files_for_prompt(self.ctx.prompt, all_files, max_files=6)
        selected = list(dict.fromkeys(prompt_files + docs[:4]))
        self._file_section = render_file_contents(ws, selected)
        self._docs_summary = "\n".join(docs) or "(no doc files found)"

    def user_prompt(self) -> str:
        return (
            f"## Existing docs (truncated)\n{self._docs_summary}\n\n"
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
                    modified, warnings = apply_search_replace_blocks(self._ws, blocks)
                except ApplyError as exc:
                    await self.ctx.emit_log("stderr", f"SR apply failed: {exc}")
                    return RunnerResult(success=False, summary=f"docs: {exc}")
                for w in warnings:
                    await self.ctx.emit_log("stderr", f"warning: {w}")

                diff = await self._ws.diff()
                if not diff.strip():
                    return RunnerResult(
                        success=False, summary="docs: SR blocks produced no net change"
                    )

                await self.ctx.emit_artifact(
                    "patch", "docs.diff", diff,
                    {"role": "docs", "format": "search-replace", "files": modified},
                )
                return RunnerResult(
                    success=True, summary=f"updated {len(modified)} doc file(s)",
                    payload={"files": modified, "format": "search-replace"},
                )

            patch = extract_diff(response_text)
            if patch:
                await self.ctx.emit_artifact(
                    "patch", "docs.diff", patch,
                    {"role": "docs", "format": "unified-diff"},
                )
                return RunnerResult(success=True, summary="docs diff produced")

            return RunnerResult(
                success=False, summary="docs: no SEARCH/REPLACE blocks or diff in output"
            )
        finally:
            await self._ws.__aexit__(None, None, None)
