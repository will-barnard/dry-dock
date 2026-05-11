"""Git operations on the orchestrator side.

Workers send back unified patches (or full file replacements); the orchestrator
is responsible for keeping a cached clone of each project repo, applying the
patch on a task branch, pushing to origin, and (optionally) opening a PR via
the GitHub API. Workers never push directly.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import httpx
import structlog

from app.config import get_settings
from app.models import Project, Task
from app.util.github import normalize_owner_repo

log = structlog.get_logger()


def _repo_dir(project: Project) -> Path:
    settings = get_settings()
    return Path(settings.repo_cache_dir) / project.slug


def _normalized_pair(project: Project) -> tuple[str, str]:
    """Heal any project row where the full URL ended up in `github_repo`. The
    project create form now prevents this, but rows from earlier deploys can
    still be wrong, and silently building a clone URL on top of them produces
    things like https://github.com/owner/https://github.com/owner/repo.git."""
    parsed = normalize_owner_repo(project.github_owner, project.github_repo)
    if parsed is None:
        return project.github_owner, project.github_repo
    return parsed


def _authed_url(project: Project) -> str:
    settings = get_settings()
    token = settings.github_token
    user = settings.github_username or "x-access-token"
    owner, repo = _normalized_pair(project)
    if not token:
        # Fall back to anonymous — clone will work for public repos but push won't.
        return f"https://github.com/{owner}/{repo}.git"
    return f"https://{user}:{token}@github.com/{owner}/{repo}.git"


async def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


# Valid line-start prefixes in a unified diff.
_DIFF_LINE_PREFIXES = (
    "diff ", "index ", "old mode", "new mode", "new file", "deleted file",
    "rename ", "similarity ", "Binary ", "---", "+++", "@@", "+", "-", " ", "\\",
)

# Headers that mark the start of a new file section (reset hunk state).
_FILE_HEADERS = ("diff ", "index ", "--- ", "+++ ", "old mode", "new mode",
                 "new file", "deleted file", "rename ", "similarity ", "Binary ")


def _clean_patch(raw: str) -> str:
    """Sanitize an LLM-generated unified diff before passing it to git apply.

    LLMs commonly produce diffs with two problems:

    1. **Empty context lines** — a context line that is empty in the source
       file must appear as `` \\n`` (a single leading space) in the diff, but
       models often emit a bare ``\\n``.  git treats those as corrupt.

    2. **Trailing prose after the last hunk** — models sometimes append an
       explanation inside the closing fence even though it isn't valid diff
       syntax.  We truncate everything after the last recognisable diff line.

    3. **CRLF line endings** — normalised to LF throughout.
    """
    # Normalise line endings.
    cleaned = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = cleaned.splitlines(keepends=True)

    # Pass 1 — fix bare empty lines inside hunks, and fix new-file hunks that
    # contain context/deletion lines (which git rejects as "depends on old contents").
    in_hunk = False
    is_new_file = False
    fixed: list[str] = []
    for line in lines:
        if line.startswith("diff "):
            in_hunk = False
            is_new_file = False
            fixed.append(line)
        elif line.startswith("new file"):
            is_new_file = True
            fixed.append(line)
        elif line.startswith(_FILE_HEADERS):
            fixed.append(line)
        elif line.startswith("@@"):
            in_hunk = True
            fixed.append(line)
        elif in_hunk:
            if line == "\n":
                # Bare empty line should be a context line.
                if is_new_file:
                    fixed.append("+\n")
                else:
                    fixed.append(" \n")
            elif is_new_file and line.startswith(" "):
                # Context line in a new-file hunk → addition.
                fixed.append("+" + line[1:])
            elif is_new_file and line.startswith("-"):
                # Deletion in a new-file hunk → doesn't exist yet, drop it.
                pass
            else:
                fixed.append(line)
        else:
            fixed.append(line)

    # Pass 2 — truncate trailing non-diff prose.
    last_valid = -1
    for i, line in enumerate(fixed):
        if line.startswith(_DIFF_LINE_PREFIXES):
            last_valid = i

    if last_valid == -1:
        return "".join(fixed)  # nothing recognisable — return as-is

    result = "".join(fixed[: last_valid + 1])
    if not result.endswith("\n"):
        result += "\n"
    return result


async def ensure_clone(project: Project) -> Path:
    """Ensure the project repo is cloned locally and up to date with origin."""
    target = _repo_dir(project)
    target.parent.mkdir(parents=True, exist_ok=True)

    if not (target / ".git").exists():
        log.info("git.clone", project=project.slug)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        code, out, err = await _run(["git", "clone", _authed_url(project), str(target)])
        if code != 0:
            raise RuntimeError(f"git clone failed: {err}")
    else:
        await _run(["git", "remote", "set-url", "origin", _authed_url(project)], cwd=target)
        code, out, err = await _run(["git", "fetch", "--prune", "origin"], cwd=target)
        if code != 0:
            log.warning("git.fetch_failed", project=project.slug, err=err)
    return target


async def apply_patch_and_push(project: Project, task: Task, patch: str) -> str:
    """Apply a unified diff on a new agent branch, then push.

    The new branch is always ``agent/<task.id>`` so it is globally unique.
    If ``task.branch_name`` is already set it was inherited from the parent task
    and is used as the *base* to branch from (so this task's changes layer on
    top of the previous agent's work).  Otherwise the project's default branch
    is the base.

    Returns the name of the newly created branch.
    """
    repo = await ensure_clone(project)
    new_branch = f"agent/{task.id}"
    base = task.branch_name or project.default_branch

    # Fetch and reset to base, then create the new agent branch from it.
    await _run(["git", "fetch", "origin", base], cwd=repo)
    await _run(["git", "checkout", "-B", new_branch, f"origin/{base}"], cwd=repo)

    # Apply patch from stdin.
    proc = await asyncio.create_subprocess_exec(
        "git",
        "apply",
        "--whitespace=nowarn",
        "--recount",  # don't trust the LLM's hunk line counts, recompute them
        "--index",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(repo),
    )
    stdout, stderr = await proc.communicate(_clean_patch(patch).encode())
    if proc.returncode != 0:
        raise RuntimeError(f"git apply failed: {stderr.decode(errors='replace')}")

    await _run(
        ["git", "-c", "user.email=bot@dry-dock", "-c", "user.name=dry-dock",
         "commit", "-m", f"agent: {task.title}\n\nTask {task.id}"],
        cwd=repo,
    )
    code, out, err = await _run(["git", "push", "-u", "origin", new_branch, "--force-with-lease"], cwd=repo)
    if code != 0:
        raise RuntimeError(f"git push failed: {err}")

    return new_branch


async def open_pull_request(project: Project, task: Task, branch: str, body: str) -> str | None:
    """Open a PR from `branch` to the project's default branch. Returns PR URL or None."""
    settings = get_settings()
    if not settings.github_token:
        return None
    url = f"https://api.github.com/repos/{project.github_owner}/{project.github_repo}/pulls"
    payload = {
        "title": f"[dry-dock] {task.title}",
        "head": branch,
        "base": project.default_branch,
        "body": body,
        "draft": False,
    }
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code >= 300:
            log.warning("github.pr_failed", status=r.status_code, body=r.text)
            return None
        return r.json().get("html_url")
