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


def _split_into_sections(lines: list[str]) -> list[list[str]]:
    """Split a diff into per-file sections (each starts with 'diff --git').

    If the model omits the ``diff --git`` header entirely but includes
    ``--- a/path`` / ``+++ b/path`` lines, synthesise a minimal header so
    the rest of the sanitizer can work on a well-formed section.
    """
    sections: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("diff ") and current:
            sections.append(current)
            current = []
        current.append(line)
    if current:
        sections.append(current)

    # For sections that have no 'diff --git' line, try to reconstruct one
    # from '+++ b/path' (or '--- a/path' as fallback).
    repaired: list[list[str]] = []
    for sec in sections:
        has_diff = any(l.startswith("diff ") for l in sec)
        if not has_diff:
            plus_line = next((l for l in sec if l.startswith("+++ b/")), None)
            minus_line = next((l for l in sec if l.startswith("--- a/")), None)
            path = None
            if plus_line:
                path = plus_line[6:].rstrip("\n")
            elif minus_line:
                path = minus_line[6:].rstrip("\n")
            if path:
                synthetic = f"diff --git a/{path} b/{path}\n"
                sec = [synthetic] + sec
        repaired.append(sec)
    return repaired


def _fix_section(section: list[str], repo: "Path | None" = None) -> list[str]:
    """Sanitize one file section of a unified diff.

    Handles five common LLM defects:
    1. CRLF endings (normalised before this is called).
    2. Bare empty lines inside hunks → space-prefixed context lines (or '+' for new files).
    3. New-file sections that use context/deletion lines instead of all-additions.
    4. Missing ``new file mode`` header when the first hunk starts at @@ -0,0.
    5. Modification-style diff for a file that doesn't actually exist in the
       working tree — detected by checking the filesystem when ``repo`` is given.
    """
    # Extract the target path from the "diff --git a/x b/x" line.
    diff_line = next((l for l in section if l.startswith("diff --git ")), "")
    parts = diff_line.rstrip("\n").split(" ")
    b_path = parts[-1][2:] if len(parts) >= 4 and parts[-1].startswith("b/") else None

    # Detect whether this is (or should be) a new-file / deleted-file section.
    has_new_file_marker = any(l.startswith("new file") for l in section)
    is_deleted_file = any(l.startswith("deleted file") for l in section)
    first_hunk = next((l for l in section if l.startswith("@@")), None)
    hunk_inferred_new = (
        not has_new_file_marker
        and not is_deleted_file
        and first_hunk is not None
        and first_hunk.startswith("@@ -0,0 ")
    )
    # If we have repo access, also check whether the file actually exists.
    fs_inferred_new = (
        not has_new_file_marker
        and not hunk_inferred_new
        and not is_deleted_file
        and repo is not None
        and b_path is not None
        and not (repo / b_path).exists()
    )
    inferred_new = hunk_inferred_new or fs_inferred_new
    is_new_file = has_new_file_marker or inferred_new

    out: list[str] = []
    in_hunk = False

    for line in section:
        if line.startswith("diff "):
            in_hunk = False
            out.append(line)
            if inferred_new:
                out.append("new file mode 100644\n")
        elif line.startswith("new file"):
            out.append(line)
        elif line.startswith("@@"):
            in_hunk = True
            out.append(line)
        elif line.startswith("--- "):
            # For any new-file section (marker or inferred) git requires
            # "--- /dev/null".  Keeping "--- a/path" causes git to look up
            # the path in the index, find context lines, and reject the patch
            # with "depends on old contents".
            if is_new_file:
                out.append("--- /dev/null\n")
            else:
                out.append(line)
        elif in_hunk:
            if is_new_file:
                if line == "\n":
                    out.append("+\n")
                elif line.startswith(" "):
                    out.append("+" + line[1:])
                elif line.startswith("-"):
                    pass  # deletion in a new file makes no sense — drop it
                else:
                    out.append(line)
            elif is_deleted_file:
                if line == "\n":
                    out.append("-\n")  # bare empty line in a deletion hunk must be removed
                elif line.startswith(" "):
                    out.append("-" + line[1:])  # context line → deletion
                elif line.startswith("+"):
                    pass  # addition in a deleted-file section makes no sense — drop it
                else:
                    out.append(line)
            else:
                if line == "\n":
                    out.append(" \n")
                else:
                    out.append(line)
        else:
            out.append(line)

    return out


def _clean_patch(raw: str, repo: "Path | None" = None) -> str:
    """Sanitize an LLM-generated unified diff before passing it to ``git apply``.

    LLMs routinely produce diffs with several classes of defect — see
    ``_fix_section`` for details.  This function normalises line endings,
    splits the diff into per-file sections, fixes each section, then strips
    any trailing non-diff prose appended inside the fence.

    Pass ``repo`` (the local clone path) so the sanitizer can check the
    filesystem and fix modification-style diffs for files that don't yet exist.
    """
    cleaned = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = cleaned.splitlines(keepends=True)

    sections = _split_into_sections(lines)
    fixed_lines: list[str] = []
    for sec in sections:
        fixed_lines.extend(_fix_section(sec, repo=repo))

    # Truncate trailing non-diff prose (lines that don't start with a valid prefix).
    last_valid = -1
    for i, line in enumerate(fixed_lines):
        if line.startswith(_DIFF_LINE_PREFIXES):
            last_valid = i

    if last_valid == -1:
        return ""  # no diff content found — caller handles gracefully

    result = "".join(fixed_lines[: last_valid + 1])
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


async def apply_patch_and_push(
    project: Project, task: Task, patch: str
) -> tuple[str, bool]:
    """Apply a unified diff and push.

    Three cases:

    - ``project.direct_push`` is True: commit directly to the project's
      default branch (main). No agent branches, no PRs. The local clone is
      rebased on top of the latest origin tip before applying so concurrent
      tasks don't collide. Returns ``(default_branch, False)``.

    - ``task.branch_name`` is already set (chained task): push onto the
      branch inherited from the parent. Returns ``(branch, False)`` so the
      caller skips opening a duplicate PR.

    - Fresh / standalone task: create ``agent/<task.id>`` off the default
      branch, push, and return ``(branch, True)`` so the caller opens the PR.
    """
    repo = await ensure_clone(project)

    if project.direct_push:
        target_branch = project.default_branch
        base = project.default_branch
        is_new_branch = False
    elif task.branch_name:
        target_branch = task.branch_name
        base = target_branch
        is_new_branch = False
    else:
        target_branch = f"agent/{task.id}"
        base = project.default_branch
        is_new_branch = True

    # Fetch and check out the working branch.
    await _run(["git", "fetch", "origin", base], cwd=repo)
    await _run(["git", "checkout", "-B", target_branch, f"origin/{base}"], cwd=repo)

    # Pre-clean: if the patch wants to create a file that's already tracked,
    # remove it from the index first so git apply sees a genuine new-file slot.
    cleaned = _clean_patch(patch, repo=repo)
    cleaned_lines = cleaned.splitlines(keepends=True)
    for sec in _split_into_sections(cleaned_lines):
        if not any(l.startswith("new file") for l in sec):
            continue
        diff_l = next((l for l in sec if l.startswith("diff --git ")), "")
        parts = diff_l.rstrip("\n").split(" ")
        b_path = parts[-1][2:] if len(parts) >= 4 and parts[-1].startswith("b/") else None
        if b_path and (repo / b_path).exists():
            log.info("git.rm_before_new_file", path=b_path)
            await _run(["git", "rm", "-f", "--", b_path], cwd=repo)

    if not cleaned.strip() or "\n@@" not in cleaned and not cleaned.startswith("@@"):
        raise RuntimeError(
            "patch apply failed: no diff content found — "
            "the model may have returned prose instead of a unified diff"
        )

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
    stdout, stderr = await proc.communicate(cleaned.encode())
    if proc.returncode != 0:
        raise RuntimeError(f"git apply failed: {stderr.decode(errors='replace')}")

    await _run(
        ["git", "-c", "user.email=bot@dry-dock", "-c", "user.name=dry-dock",
         "commit", "-m", f"agent: {task.title}\n\nTask {task.id}"],
        cwd=repo,
    )
    # --force-with-lease is safe even for the chained-branch case because we
    # just fetched and checked out origin's tip; if a sibling task pushed
    # between the fetch and our push, the lease will reject and we'll fail
    # the task cleanly rather than silently overwrite.
    if project.direct_push:
        # For direct-to-main pushes, rebase onto the freshly-fetched tip to
        # handle concurrent agent commits, then push without force.
        await _run(["git", "fetch", "origin", target_branch], cwd=repo)
        rebase_code, _, rebase_err = await _run(
            ["git", "rebase", f"origin/{target_branch}"], cwd=repo
        )
        if rebase_code != 0:
            raise RuntimeError(f"git rebase failed (concurrent push conflict): {rebase_err}")
        code, out, err = await _run(
            ["git", "push", "origin", target_branch], cwd=repo
        )
    else:
        code, out, err = await _run(
            ["git", "push", "-u", "origin", target_branch, "--force-with-lease"],
            cwd=repo,
        )
    if code != 0:
        raise RuntimeError(f"git push failed: {err}")

    return target_branch, is_new_branch


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
