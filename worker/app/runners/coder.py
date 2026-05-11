"""Coder runner — produce a unified diff for a code change.

Strategy: clone the repo, gather a lightweight file-tree summary, ask the
model for a unified diff, ship the diff back as a `patch` artifact. The
orchestrator validates by trying to `git apply` it.
"""
from __future__ import annotations

from app.git_workspace import GitWorkspace
from app.runners.base import BaseRunner, RunnerResult, extract_diff


_CODER_INSTRUCTIONS = """\
You will write code by emitting a unified diff against the project's current
default branch. Follow these rules strictly:

1. Output a single ```diff fenced code block. Nothing else after it.
2. The diff must apply cleanly with `git apply` from the repo root.
3. Use proper `--- a/path` and `+++ b/path` headers. For new files, use `--- /dev/null`.
4. Keep changes minimal and focused on the task. Don't drive-by refactor.
5. Before the diff, write a short plan in plain prose explaining what you'll change. Then the diff.
"""


class CoderRunner(BaseRunner):
    role = "coder"

    def system_prompt(self) -> str:
        return super().system_prompt() + "\n\n" + _CODER_INSTRUCTIONS

    async def setup(self) -> None:
        # Clone the repo and stash a file-tree summary into the user prompt
        # context so the model knows what it's working with. For MVP we keep
        # this very small; in the next iteration we'll switch to embeddings.
        ws = GitWorkspace(
            self.ctx.project["github_owner"],
            self.ctx.project["github_repo"],
            self.ctx.project.get("default_branch", "main"),
        )
        self._ws = ws
        await ws.__aenter__()

        files = [f for f in ws.list_files() if not f.startswith((".git/", "node_modules/", ".venv/"))]
        # Cap the tree dump so we don't blow context.
        head = files[:200]
        tree = "\n".join(head)
        if len(files) > 200:
            tree += f"\n… and {len(files) - 200} more files"
        self._tree_summary = tree
        await self.ctx.emit_log("system", f"cloned repo, {len(files)} files")

    def user_prompt(self) -> str:
        return (
            f"Repository: {self.ctx.project['github_owner']}/{self.ctx.project['github_repo']}\n"
            f"Default branch: {self.ctx.project.get('default_branch', 'main')}\n\n"
            f"Repo file tree (truncated):\n{self._tree_summary}\n\n"
            f"## Task\n{self.ctx.prompt}\n"
        )

    async def finalize(self, response_text: str) -> RunnerResult:
        try:
            patch = extract_diff(response_text)
            if not patch:
                await self.ctx.emit_log("stderr", "no diff block found in coder response")
                await self.ctx.emit_artifact("text", "raw_response.txt", response_text, {})
                return RunnerResult(success=False, summary="coder: no diff block produced")

            # Emit the patch — the orchestrator will try to apply + push it.
            await self.ctx.emit_artifact("patch", "agent.diff", patch, {"role": "coder"})
            # Also emit the explanatory prose ahead of the diff as a text summary.
            preface = response_text.split("```", 1)[0].strip()
            await self.ctx.emit_artifact("text", "plan.txt", preface, {})
            return RunnerResult(
                success=True,
                summary=preface[:300] if preface else "coder produced a patch",
                payload={"diff_lines": patch.count("\n")},
            )
        finally:
            await self._ws.__aexit__(None, None, None)
