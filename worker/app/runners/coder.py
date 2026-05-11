"""Coder runner — emit SEARCH/REPLACE blocks, apply locally, ship git's diff.

Old design asked the model to produce a unified diff and shipped it raw to the
orchestrator. That fails for smaller models because diffs require exact line
counts and faithful context lines, and we weren't even giving the model the
files to look at. New design:

  1. Clone the repo (inherit branch from parent task if chained).
  2. Pick the files most likely to be touched and put their contents in the
     user prompt so the model can SEARCH against reality.
  3. Ask for SEARCH/REPLACE blocks.
  4. Apply locally; ask `git diff --cached` for the canonical unified diff.
  5. Ship that diff as the `patch` artifact (orchestrator's git apply now
     gets a diff that was produced by git, so it always applies).

If the model produces a real diff instead of SR blocks, we still try to use
it as a fallback so the runner is forgiving of model style.
"""
from __future__ import annotations

import structlog

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

log = structlog.get_logger()


_CODER_INSTRUCTIONS = """\
You are writing or modifying code. Before the changes, write a brief plain-prose
plan (3–6 sentences) describing what you're going to change and why. Then
emit your edits as SEARCH/REPLACE blocks. Do not output a unified diff.
""" + "\n" + SEARCH_REPLACE_INSTRUCTIONS


class CoderRunner(BaseRunner):
    role = "coder"

    def system_prompt(self) -> str:
        return super().system_prompt() + "\n\n" + _CODER_INSTRUCTIONS

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

        # Pick a handful of files the model most likely needs to see, then load
        # their contents into the prompt. Without this the model is editing
        # blind and produces SEARCH blocks that don't match anything.
        self._target_files = relevant_files_for_prompt(self.ctx.prompt, all_files, max_files=8)
        self._file_section = render_file_contents(ws, self._target_files)

        # Cap the tree dump independently — it gives the model awareness of
        # files it may want to reference even if it didn't see their content.
        tree_head = all_files[:200]
        self._tree = "\n".join(tree_head)
        if len(all_files) > 200:
            self._tree += f"\n… and {len(all_files) - 200} more files"

        await self.ctx.emit_log(
            "system",
            f"cloned repo on branch={branch}, {len(all_files)} files, "
            f"loaded {len(self._target_files)} into prompt",
        )

    def user_prompt(self) -> str:
        return (
            f"Repository: {self.ctx.project['github_owner']}/{self.ctx.project['github_repo']}\n"
            f"Branch: {self.ctx.branch_name or self.ctx.project.get('default_branch', 'main')}\n\n"
            f"## Repo file tree (truncated)\n{self._tree}\n\n"
            f"## Current contents of the most likely target files\n"
            f"Use these to write SEARCH blocks that match the file EXACTLY. "
            f"If you need to edit a file not shown here, mention its path in "
            f"your plan and I'll include it next round.\n\n"
            f"{self._file_section or '(no target files matched the task by name)'}\n\n"
            f"## Task\n{self.ctx.prompt}\n"
        )

    async def finalize(self, response_text: str) -> RunnerResult:
        try:
            # Always save the raw response so the user can see what the model
            # actually produced when things go sideways.
            await self.ctx.emit_artifact("text", "raw_response.txt", response_text, {})

            blocks = extract_search_replace_blocks(response_text)
            if blocks:
                try:
                    modified, warnings = apply_search_replace_blocks(self._ws, blocks)
                except ApplyError as exc:
                    await self.ctx.emit_log("stderr", f"SR apply failed: {exc}")
                    return RunnerResult(
                        success=False,
                        summary=f"coder: SEARCH/REPLACE apply failed — {exc}",
                    )
                for w in warnings:
                    await self.ctx.emit_log("stderr", f"warning: {w}")

                diff = await self._ws.diff()
                if not diff.strip():
                    return RunnerResult(
                        success=False,
                        summary="coder: SR blocks parsed but produced no net change",
                    )

                await self.ctx.emit_artifact(
                    "patch", "agent.diff", diff,
                    {"role": "coder", "format": "search-replace", "files": modified},
                )
                preface = response_text.split("<<<<<<<", 1)[0].strip()
                if preface:
                    await self.ctx.emit_artifact("text", "plan.txt", preface, {})
                return RunnerResult(
                    success=True,
                    summary=preface[:300] if preface else f"edited {len(modified)} file(s)",
                    payload={
                        "files": modified,
                        "diff_lines": diff.count("\n"),
                        "format": "search-replace",
                    },
                )

            # Fallback: model produced a real unified diff. Use it raw.
            patch = extract_diff(response_text)
            if patch:
                await self.ctx.emit_artifact(
                    "patch", "agent.diff", patch,
                    {"role": "coder", "format": "unified-diff"},
                )
                preface = response_text.split("```", 1)[0].strip()
                if preface:
                    await self.ctx.emit_artifact("text", "plan.txt", preface, {})
                return RunnerResult(
                    success=True,
                    summary=preface[:300] if preface else "coder produced a unified diff",
                    payload={"diff_lines": patch.count("\n"), "format": "unified-diff"},
                )

            await self.ctx.emit_log(
                "stderr",
                "coder produced neither SEARCH/REPLACE blocks nor a valid diff",
            )
            return RunnerResult(
                success=False,
                summary="coder: model output had no SEARCH/REPLACE blocks or diff",
            )
        finally:
            await self._ws.__aexit__(None, None, None)
