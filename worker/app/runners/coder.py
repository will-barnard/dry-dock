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
    render_contract_section,
    render_file_contents,
    request_sr_retry,
    task_contract,
    task_target_files,
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

        # Planner-supplied target_files take precedence over the heuristic.
        # If the planner gave us a list and at least one path exists, use
        # exactly those — no guessing, no speculative file loading.
        planned = task_target_files(self.ctx.payload, all_files)
        if planned:
            self._target_files = planned
            file_source = "plan"
        else:
            self._target_files = relevant_files_for_prompt(
                self.ctx.prompt, all_files, max_files=20
            )
            file_source = "heuristic"
        self._file_section = render_file_contents(ws, self._target_files)

        # Cap the tree dump independently — it gives the model awareness of
        # files it may want to reference even if it didn't see their content.
        self._tree = "\n".join(all_files)
        self._contract_section = render_contract_section(task_contract(self.ctx.payload))

        await self.ctx.emit_log(
            "system",
            f"cloned repo on branch={branch}, {len(all_files)} files, "
            f"loaded {len(self._target_files)} into prompt (source={file_source})",
        )

    def user_prompt(self) -> str:
        return (
            f"Repository: {self.ctx.project['github_owner']}/{self.ctx.project['github_repo']}\n"
            f"Branch: {self.ctx.branch_name or self.ctx.project.get('default_branch', 'main')}\n\n"
            f"{self._contract_section}"
            f"## Repo file tree\n{self._tree}\n\n"
            f"## Current contents of the target files\n"
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

            # Try SR blocks first, with one retry-with-file-contents on apply failure.
            current_response = response_text
            last_error: ApplyError | None = None
            success_payload: tuple[list[str], list[str], str] | None = None

            for attempt in range(2):
                blocks = extract_search_replace_blocks(current_response)
                if not blocks:
                    break  # no SR blocks; fall through to diff fallback below
                try:
                    modified, warnings = apply_search_replace_blocks(self._ws, blocks)
                except ApplyError as exc:
                    last_error = exc
                    if attempt + 1 >= 2:
                        break  # out of retries
                    failed_files = list(dict.fromkeys(b[0] for b in blocks))
                    await self.ctx.emit_log(
                        "system",
                        f"SR apply failed ({exc}); retrying with actual contents of "
                        f"{len(failed_files)} file(s) loaded into the prompt",
                    )
                    new_response = await request_sr_retry(
                        self, self.user_prompt(), current_response,
                        failed_files, str(exc), self._ws,
                    )
                    if not new_response.strip():
                        await self.ctx.emit_log("stderr", "retry call returned no content")
                        break
                    await self.ctx.emit_artifact(
                        "text", f"retry_response_{attempt + 1}.txt", new_response, {}
                    )
                    current_response = new_response
                    continue
                else:
                    for w in warnings:
                        await self.ctx.emit_log("stderr", f"warning: {w}")
                    success_payload = (modified, warnings, current_response)
                    break

            if success_payload is not None:
                modified, _warnings, success_response = success_payload
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
                preface = success_response.split("<<<<<<<", 1)[0].strip()
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

            if last_error is not None:
                await self.ctx.emit_log("stderr", f"SR apply failed after retry: {last_error}")
                return RunnerResult(
                    success=False,
                    summary=f"coder: SEARCH/REPLACE apply failed — {last_error}",
                )

            # No SR blocks ever found. Fallback: maybe the model produced a real
            # unified diff. Use it raw.
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
