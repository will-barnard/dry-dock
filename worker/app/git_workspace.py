"""Per-task git workspace using ephemeral worktrees.

Each job gets a fresh clone (shallow, default branch) under WORKTREE_ROOT.
Runners read from the working copy and emit unified diffs against the base
branch. The orchestrator applies + pushes the diff; workers never push.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import structlog

from app.config import get_settings

log = structlog.get_logger()


async def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


class GitWorkspace:
    """Context manager: clone the repo for the duration of one job."""

    def __init__(self, github_owner: str, github_repo: str, default_branch: str) -> None:
        self.owner = github_owner
        self.repo = github_repo
        self.default_branch = default_branch
        self.path: Path | None = None
        self._tmp: tempfile.TemporaryDirectory | None = None

    async def __aenter__(self) -> "GitWorkspace":
        settings = get_settings()
        Path(settings.worktree_root).mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=settings.worktree_root, prefix=f"{self.repo}-")
        self.path = Path(self._tmp.name) / "repo"
        url = f"https://github.com/{self.owner}/{self.repo}.git"
        code, _, err = await _run(["git", "clone", "--depth", "1", "--branch", self.default_branch, url, str(self.path)])
        if code != 0:
            raise RuntimeError(f"git clone failed: {err}")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._tmp is not None:
            try:
                self._tmp.cleanup()
            except Exception:
                if self.path and self.path.parent.exists():
                    shutil.rmtree(self.path.parent, ignore_errors=True)

    async def diff(self) -> str:
        """Return a unified diff of any uncommitted changes."""
        assert self.path is not None
        # Stage everything (including new files) so the diff includes them.
        await _run(["git", "add", "-A"], cwd=self.path)
        code, out, err = await _run(["git", "diff", "--cached"], cwd=self.path)
        if code != 0:
            raise RuntimeError(f"git diff failed: {err}")
        return out

    def read(self, relpath: str) -> str:
        assert self.path is not None
        p = self.path / relpath
        return p.read_text(encoding="utf-8")

    def write(self, relpath: str, content: str) -> None:
        assert self.path is not None
        p = self.path / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def list_files(self, glob: str = "**/*") -> list[str]:
        assert self.path is not None
        return [str(p.relative_to(self.path)) for p in self.path.glob(glob) if p.is_file() and ".git" not in p.parts]
