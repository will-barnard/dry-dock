"""Parse the various forms users paste for a GitHub repo into (owner, repo)."""
from __future__ import annotations

import re

# Accepts:
#   owner/repo
#   owner/repo.git
#   https://github.com/owner/repo
#   https://github.com/owner/repo.git
#   git@github.com:owner/repo.git
#   git://github.com/owner/repo.git
_GH_RE = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:|git://github\.com/)?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"/"
    r"(?P<repo>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:\.git)?/?$"
)


def parse_github_ref(value: str) -> tuple[str, str] | None:
    """Return (owner, repo) for a GitHub URL or ``owner/repo`` shorthand.

    Returns None if the string can't be confidently parsed — callers should
    surface a 400 with a helpful message in that case rather than letting the
    bad value flow into clone commands.
    """
    s = (value or "").strip()
    if not s:
        return None
    m = _GH_RE.match(s)
    if not m:
        return None
    repo = m.group("repo")
    # The regex's repo character class includes '.' so it can greedily consume
    # a trailing .git suffix instead of leaving it for the optional group.
    # Strip it explicitly so callers that append .git don't produce .git.git.
    if repo.endswith(".git"):
        repo = repo[:-4]
    return m.group("owner"), repo


def normalize_owner_repo(owner: str, repo: str) -> tuple[str, str] | None:
    """Two-field variant: if `repo` looks URL-y, treat the whole thing as a
    full ref and ignore `owner`. Otherwise return cleaned values."""
    owner = (owner or "").strip()
    repo = (repo or "").strip()
    if "/" in repo or ":" in repo:
        return parse_github_ref(repo)
    if not owner or not repo:
        return None
    parsed = parse_github_ref(f"{owner}/{repo}")
    return parsed
