"""Per-task git workspace using ephemeral worktrees.

Each job gets a fresh clone (shallow, default branch) under WORKTREE_ROOT.
Runners read from the working copy and emit unified diffs against the base
branch. The orchestrator applies + pushes the diff; workers never push.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path

import structlog

from app.config import get_settings

log = structlog.get_logger()


_PREFIX_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

# Same parse rules the backend uses on project create — duplicated here so the
# worker can self-heal if the orchestrator hands it a project row where the
# full URL was stuffed into the `repo` field (early bad data, copy-paste
# errors). If the repo string looks URL-y we extract owner/repo from it and
# override the passed-in owner.
_GH_REF_RE = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:|git://github\.com/)?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"/"
    r"(?P<repo>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:\.git)?/?$"
)


def _safe_prefix(value: str) -> str:
    """Make a string safe to pass as tempfile prefix."""
    cleaned = _PREFIX_SAFE_RE.sub("_", value or "")
    return (cleaned[:48] or "repo") + "-"


def _normalize_ref(owner: str, repo: str) -> tuple[str, str]:
    """If `repo` looks like a URL or owner/repo path, parse it and let the
    parsed values win over the passed-in `owner`. Otherwise return as-is.
    Also strips a bare .git suffix that may have been stored in the project row
    — the clone URL builder always appends .git, so leaving it in produces
    double-suffixed URLs like zoo-sandbox.git.git."""
    raw = (repo or "").strip()
    if "/" in raw or ":" in raw:
        m = _GH_REF_RE.match(raw)
        if m:
            return m.group("owner"), m.group("repo")
    if raw.endswith(".git"):
        raw = raw[:-4]
    return owner, raw


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
        owner, repo = _normalize_ref(github_owner, github_repo)
        if (owner, repo) != (github_owner, github_repo):
            log.warning(
                "git_workspace.normalized_ref",
                from_owner=github_owner, from_repo=github_repo,
                to_owner=owner, to_repo=repo,
            )
        self.owner = owner
        self.repo = repo
        self.default_branch = default_branch
        self.path: Path | None = None
        self._tmp: tempfile.TemporaryDirectory | None = None

    async def __aenter__(self) -> "GitWorkspace":
        settings = get_settings()
        Path(settings.worktree_root).mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(
            dir=settings.worktree_root, prefix=_safe_prefix(self.repo)
        )
        self.path = Path(self._tmp.name) / "repo"
        # Use a credentialed URL when a token is available so we can clone
        # private repos. Token in the URL is fine over HTTPS for ephemeral
        # clones; git won't write it to ~/.netrc unless asked.
        if settings.github_token:
            user = settings.github_username or "x-access-token"
            url = f"https://{user}:{settings.github_token}@github.com/{self.owner}/{self.repo}.git"
        else:
            url = f"https://github.com/{self.owner}/{self.repo}.git"
        code, _, err = await _run(["git", "clone", "--depth", "1", "--branch", self.default_branch, url, str(self.path)])
        if code != 0:
            # Scrub the token from any error we surface upward.
            safe_err = err.replace(settings.github_token, "***") if settings.github_token else err
            raise RuntimeError(f"git clone failed: {safe_err}")
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
