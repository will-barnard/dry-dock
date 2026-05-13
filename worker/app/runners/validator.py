"""Validator runner — run the project's validation commands on the cloned branch.

This is the integration gate: after a coder/refactorer modifies code, a
`validate` task runs the project's declared check commands (build, lint,
typecheck, test) against the resulting branch. If anything exits non-zero,
the validator emits a failure and the orchestrator auto-requeues the parent
task with the failure output appended to its prompt — closing the
write-validate-fix loop without human intervention.

Configuration lives on the Project row (Project.validate_commands), and the
orchestrator forwards it to the worker in claim_grant.project.validate_commands.
The validator NEVER reads its own list — that would defeat the per-project
contract.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import structlog

from app.git_workspace import GitWorkspace
from app.runners.base import (
    BaseRunner,
    RunnerResult,
    render_contract_section,
    task_contract,
)

log = structlog.get_logger()


# Per-command timeout. A build that takes longer than this is probably stuck.
_PER_COMMAND_TIMEOUT_SECONDS = 600


class ValidatorRunner(BaseRunner):
    role = "validator"

    # The validator does no LLM inference itself — it's a deterministic
    # runner. We still inherit BaseRunner for the WS plumbing, but we
    # override `run` entirely to skip the chat-stream loop.

    async def setup(self) -> None:
        branch = self.ctx.branch_name or self.ctx.project.get("default_branch", "main")
        self._ws = GitWorkspace(
            self.ctx.project["github_owner"],
            self.ctx.project["github_repo"],
            branch,
        )
        await self._ws.__aenter__()
        await self.ctx.emit_log(
            "system",
            f"cloned repo on branch={branch} for validation",
        )

    async def run(self) -> RunnerResult:
        await self.setup()
        try:
            commands = self._commands()
            if not commands:
                await self.ctx.emit_log(
                    "system",
                    "no validate_commands configured for this project — "
                    "validator passes vacuously",
                )
                await self.ctx.emit_artifact(
                    "test_report", "validation.md",
                    "# Validation report\n\nNo `validate_commands` are configured "
                    "for this project. Set them on the project page (Settings → "
                    "Validate commands) to enable the integration gate.\n",
                    {"verdict": "skipped"},
                )
                return RunnerResult(
                    success=True, summary="validator skipped (no commands)",
                    payload={"verdict": "skipped", "results": []},
                )

            results: list[dict[str, Any]] = []
            all_passed = True
            for cmd in commands:
                await self.ctx.emit_log("system", f"running: {cmd}")
                result = await self._run_command(cmd)
                results.append(result)
                # Stream the captured output back so the dashboard log shows
                # the actual build/test output, not just a summary.
                if result["stdout"]:
                    await self.ctx.emit_log("stdout", result["stdout"])
                if result["stderr"]:
                    await self.ctx.emit_log("stderr", result["stderr"])
                if result["exit"] != 0:
                    all_passed = False
                    await self.ctx.emit_log(
                        "system",
                        f"FAIL: `{cmd}` exited {result['exit']}; stopping",
                    )
                    break  # short-circuit on first failure — no point continuing
                await self.ctx.emit_log("system", f"PASS: `{cmd}`")

            report = self._format_report(results, all_passed)
            await self.ctx.emit_artifact(
                "test_report", "validation.md", report,
                {"verdict": "pass" if all_passed else "fail"},
            )

            if all_passed:
                return RunnerResult(
                    success=True,
                    summary=f"validator: all {len(results)} command(s) passed",
                    payload={"verdict": "pass", "results": results},
                )
            failed = next(r for r in results if r["exit"] != 0)
            return RunnerResult(
                success=False,
                summary=(
                    f"validator: `{failed['cmd']}` exited {failed['exit']}"
                ),
                payload={"verdict": "fail", "results": results},
            )
        finally:
            await self._ws.__aexit__(None, None, None)

    # ── helpers ────────────────────────────────────────────────────

    def _resolve_cwd(self, cmd: str) -> Path:
        """Return the best working directory for this command.

        For npm/yarn/pnpm/npx commands: if there's no package.json at the
        repo root, scan immediate subdirectories for one and use the first
        found (alphabetically, preferring 'frontend', 'client', 'web').
        All other commands always run from the repo root.
        """
        assert self._ws.path is not None
        root = self._ws.path
        _NPM_PREFIXES = ("npm ", "npm\t", "yarn ", "yarn\t", "pnpm ", "pnpm\t", "npx ")
        if not any(cmd.lstrip().startswith(p) for p in _NPM_PREFIXES):
            return root
        if (root / "package.json").exists():
            return root
        # Prefer well-known names, then fall back alphabetically.
        _PREFERRED = ("frontend", "client", "web", "app", "ui")
        subdirs = sorted(
            (d for d in root.iterdir() if d.is_dir() and (d / "package.json").exists()),
            key=lambda d: (_PREFERRED.index(d.name) if d.name in _PREFERRED else len(_PREFERRED), d.name),
        )
        return subdirs[0] if subdirs else root

    def _commands(self) -> list[str]:
        raw = self.ctx.project.get("validate_commands") or []
        if not isinstance(raw, list):
            return []
        return [str(c).strip() for c in raw if isinstance(c, str) and str(c).strip()]

    async def _run_command(self, cmd: str) -> dict[str, Any]:
        """Run one shell command inside the worktree. Captures stdout/stderr
        with a per-command timeout so a hung build doesn't pin a worker."""
        assert self._ws.path is not None
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "CI": "1",  # many tools use this to disable interactive output
        }
        cwd = self._resolve_cwd(cmd)
        if cwd != self._ws.path:
            await self.ctx.emit_log(
                "system",
                f"  note: no package.json at repo root; running from {cwd.relative_to(self._ws.path)}",
            )
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=env,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=_PER_COMMAND_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {"cmd": cmd, "exit": 124, "stdout": "", "stderr": "(timeout)"}
            # Cap captured output so a chatty build doesn't push 50MB across the WS.
            return {
                "cmd": cmd,
                "exit": proc.returncode or 0,
                "stdout": stdout_b.decode(errors="replace")[-16000:],
                "stderr": stderr_b.decode(errors="replace")[-8000:],
            }
        except FileNotFoundError as exc:
            return {"cmd": cmd, "exit": 127, "stdout": "", "stderr": str(exc)}

    @staticmethod
    def _format_report(results: list[dict[str, Any]], all_passed: bool) -> str:
        lines: list[str] = [
            "# Validation report",
            "",
            f"Verdict: **{'pass' if all_passed else 'fail'}**",
            "",
        ]
        for r in results:
            status = "PASS" if r["exit"] == 0 else f"FAIL (exit {r['exit']})"
            lines.append(f"## `{r['cmd']}` — {status}")
            lines.append("")
            if r["stdout"]:
                lines.append("```")
                lines.append(r["stdout"])
                lines.append("```")
            if r["stderr"]:
                lines.append("**stderr:**")
                lines.append("```")
                lines.append(r["stderr"])
                lines.append("```")
            lines.append("")
        return "\n".join(lines)
