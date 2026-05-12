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
    render_contract_section,
    render_file_contents,
    request_format_retry,
    request_sr_retry,
    task_contract,
    task_target_files,
)


_DOCS_INSTRUCTIONS = """\
You write project documentation. Match the project's existing style. Touch
only documentation files (README, docs/, ADRs, code comments). Emit
SEARCH/REPLACE blocks — do not just write prose, the platform can only
apply changes wrapped in the SR format below.

Worked example. If a task asks you to add a "Quickstart" section to README.md,
your entire output should look like this (the prose plan above the block is
optional but the block is mandatory):

    Adding a Quickstart section with install + run commands.

    README.md
    <<<<<<< SEARCH
    ## Installation

    Clone the repo and run `make`.
    =======
    ## Installation

    Clone the repo and run `make`.

    ## Quickstart

    ```bash
    git clone https://github.com/me/proj
    cd proj
    make run
    ```
    >>>>>>> REPLACE

For a brand-new doc file, leave SEARCH empty:

    docs/SETUP.md
    <<<<<<< SEARCH
    =======
    # Setup

    1. Install deps with `npm install`.
    2. ...
    >>>>>>> REPLACE
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
        ]
        planned = task_target_files(self.ctx.payload, all_files)
        if planned:
            selected = list(dict.fromkeys(planned + docs))
        else:
            prompt_files = relevant_files_for_prompt(
                self.ctx.prompt, all_files, max_files=20
            )
            selected = list(dict.fromkeys(prompt_files + docs))
        self._file_section = render_file_contents(ws, selected)
        self._docs_summary = "\n".join(docs) or "(no doc files found)"
        self._contract_section = render_contract_section(task_contract(self.ctx.payload))

    def user_prompt(self) -> str:
        return (
            f"{self._contract_section}"
            f"## Existing docs\n{self._docs_summary}\n\n"
            f"## Current contents of target files\n"
            f"{self._file_section or '(nothing matched by name)'}\n\n"
            f"## Task\n{self.ctx.prompt}\n"
        )

    async def finalize(self, response_text: str) -> RunnerResult:
        try:
            await self.ctx.emit_artifact("text", "raw_response.txt", response_text, {})

            current_response = response_text
            last_apply_error: ApplyError | None = None

            # Up to 2 attempts. On no-blocks we retry by asking for the format;
            # on SR-apply failure we retry by handing over the real file contents.
            for attempt in range(2):
                blocks = extract_search_replace_blocks(current_response)

                if not blocks:
                    # First time we see no blocks, ask the model to reformat.
                    if attempt + 1 >= 2:
                        break
                    await self.ctx.emit_log(
                        "system",
                        "no SR blocks in response; re-prompting for the format",
                    )
                    new_response = await request_format_retry(
                        self, self.user_prompt(), current_response,
                    )
                    if not new_response.strip():
                        break
                    await self.ctx.emit_artifact(
                        "text", f"retry_response_{attempt + 1}.txt", new_response, {}
                    )
                    current_response = new_response
                    continue

                try:
                    modified, warnings = apply_search_replace_blocks(self._ws, blocks)
                except ApplyError as exc:
                    last_apply_error = exc
                    if attempt + 1 >= 2:
                        break
                    failed_files = list(dict.fromkeys(b[0] for b in blocks))
                    await self.ctx.emit_log(
                        "system",
                        f"SR apply failed ({exc}); retrying with file contents",
                    )
                    new_response = await request_sr_retry(
                        self, self.user_prompt(), current_response,
                        failed_files, str(exc), self._ws,
                    )
                    if not new_response.strip():
                        break
                    await self.ctx.emit_artifact(
                        "text", f"retry_response_{attempt + 1}.txt", new_response, {}
                    )
                    current_response = new_response
                    continue

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

            if last_apply_error is not None:
                await self.ctx.emit_log("stderr", f"SR apply failed: {last_apply_error}")
                return RunnerResult(success=False, summary=f"docs: {last_apply_error}")

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
