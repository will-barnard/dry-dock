"""Base class for role-specific runners.

A runner gets a RunnerContext (task fields + a callback for log/artifact
emission) and returns a RunnerResult. Subclasses override system_prompt(),
build_messages(), and optionally produce_artifacts().
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.config import get_settings
from app.git_workspace import GitWorkspace
from app.ollama_client import get_provider

log = structlog.get_logger()


@dataclass
class RunnerContext:
    task_id: str
    run_id: str
    title: str
    prompt: str
    project: dict[str, Any]
    payload: dict[str, Any]
    preferred_model: str | None
    emit_log: Callable[[str, str], Awaitable[None]]  # (stream, body)
    emit_artifact: Callable[[str, str, str, dict], Awaitable[None]]  # (kind, name, content, metadata)
    # Branch inherited from a parent task (e.g. previous coder in a chain).
    # When set, runners should use this as the working branch instead of the
    # project's default branch.
    branch_name: str | None = None


@dataclass
class RunnerResult:
    success: bool
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0


class BaseRunner:
    role: str = "base"

    def __init__(self, ctx: RunnerContext):
        self.ctx = ctx
        self.provider = get_provider()
        self.model = ctx.preferred_model or get_settings().default_model

    # ── Subclass hooks ──────────────────────────────────────────────

    def system_prompt(self) -> str:
        project_prompt = self.ctx.project.get("system_prompt") or ""
        return (
            f"You are a {self.role} agent in the dry-dock multi-agent platform. "
            f"Be concise, deliberate, and produce exactly the output format requested.\n\n"
            f"{project_prompt}"
        )

    def user_prompt(self) -> str:
        return self.ctx.prompt

    async def setup(self) -> None:
        """Called before the LLM is invoked. Subclasses may clone the repo here."""
        return

    async def finalize(self, response_text: str) -> RunnerResult:
        """Called after the LLM returns. Subclasses parse the response and emit
        their domain-specific artifacts here."""
        await self.ctx.emit_artifact("text", "response.txt", response_text, {})
        return RunnerResult(success=True, summary=response_text[:300])

    # ── Driver ──────────────────────────────────────────────────────

    async def run(self) -> RunnerResult:
        await self.setup()
        messages = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": self.user_prompt()},
        ]
        await self.ctx.emit_log("system", f"model={self.model} role={self.role}")

        chunks: list[str] = []
        tokens_in = 0
        tokens_out = 0
        log_buf: list[str] = []
        try:
            async for ev in self.provider.chat_stream(self.model, messages):
                msg = ev.get("message") or {}
                piece = msg.get("content") or ""
                if piece:
                    chunks.append(piece)
                    log_buf.append(piece)
                    # Flush the buffer whenever we hit a newline so the live
                    # log shows full lines rather than individual tokens.
                    if "\n" in piece:
                        await self.ctx.emit_log("stdout", "".join(log_buf))
                        log_buf = []
                if ev.get("done"):
                    if log_buf:
                        await self.ctx.emit_log("stdout", "".join(log_buf))
                        log_buf = []
                    tokens_in = ev.get("prompt_eval_count", 0) or 0
                    tokens_out = ev.get("eval_count", 0) or 0
        except Exception as exc:
            log.exception("runner.chat_failed")
            await self.ctx.emit_log("stderr", f"chat failed: {exc}")
            return RunnerResult(success=False, summary=f"chat failed: {exc}")

        response = "".join(chunks)
        result = await self.finalize(response)
        result.tokens_in = tokens_in
        result.tokens_out = tokens_out
        return result


# ── Shared helpers ──────────────────────────────────────────────────


_FENCE_RE = re.compile(r"```([a-zA-Z0-9_\-+.]*)\n(.*?)```", re.DOTALL)


def extract_fenced_blocks(text: str) -> list[tuple[str, str]]:
    """Return [(language_tag, body), ...] for every fenced code block."""
    return [(m.group(1), m.group(2)) for m in _FENCE_RE.finditer(text)]


def extract_diff(text: str) -> str | None:
    """Pull the first fenced block that looks like a unified diff.

    Accepts blocks tagged as ``diff`` or ``patch``, any block whose content
    starts with canonical diff headers, *and* any block (regardless of
    language tag) that contains ``--- `` / ``+++ `` diff hunk markers — which
    catches models that label their output ```python, ```typescript, etc.
    """
    for tag, body in extract_fenced_blocks(text):
        if tag.lower() in {"diff", "patch"}:
            return body
        stripped = body.lstrip()
        if stripped.startswith(("diff --git", "--- ", "Index: ")):
            return body
        # Fallback: any block containing unified diff hunk markers
        if "--- " in body and "+++ " in body and "\n@@" in body:
            return body
    return None


# ── SEARCH/REPLACE block format ─────────────────────────────────────
#
# Why this format over unified diffs:
#
#   - No line numbers or hunk headers for the model to get wrong.
#   - No context-line bookkeeping — exact substring match is all we need.
#   - New files (empty SEARCH) and deletions (empty REPLACE) fall out naturally.
#   - We apply locally and let `git diff` produce the canonical patch the
#     orchestrator pushes, so what crosses the wire is always a valid diff
#     by construction.
#
# Block shape:
#
#   path/to/file.py
#   <<<<<<< SEARCH
#   ...exact bytes currently in the file...
#   =======
#   ...bytes to replace them with...
#   >>>>>>> REPLACE
#
# A whole block must live inside a single ``` fence (any language tag) OR
# appear at top level — the parser handles both so models can be loose.

# Match the head/middle/tail markers; the filename is whatever appeared on the
# preceding line. We capture greedy bodies non-greedily and stop on the
# closing marker. DOTALL so '.' matches newlines inside the bodies.
_SR_RE = re.compile(
    r"<{5,}\s*SEARCH\s*\n"
    r"(?P<search>.*?)\n?"
    r"={5,}\s*\n"
    r"(?P<replace>.*?)\n?"
    r">{5,}\s*REPLACE\s*$",
    re.DOTALL | re.MULTILINE,
)


def extract_search_replace_blocks(text: str) -> list[tuple[str, str, str]]:
    """Pull every (filename, search, replace) tuple from a model response.

    Filename is the last non-empty non-fence line preceding each `<<<<<<<`
    marker. Works whether blocks are inside fenced code blocks or at the
    top level — we strip fences first and parse the whole text.
    """
    # Strip ``` fences (any language) by removing the lines themselves while
    # keeping their contents. Same effect as concatenating every fenced block
    # with the unfenced prose around it.
    flat_lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        flat_lines.append(line)
    flat = "\n".join(flat_lines)

    blocks: list[tuple[str, str, str]] = []
    for m in _SR_RE.finditer(flat):
        # Walk backward from the match start to find the filename line.
        head = flat[: m.start()]
        filename = ""
        for prev in reversed(head.splitlines()):
            stripped = prev.strip().lstrip("#").strip()
            if not stripped:
                continue
            # Clean backticks/punctuation before the path-vs-prose test.
            candidate = stripped.rstrip(":` ").lstrip("`").strip()
            # A real file path has no spaces and contains at least one
            # path-like character. Prose sentences (even ones containing
            # "Vue.js" or a slash) always have spaces, so this rejects them.
            if " " not in candidate and (
                "/" in candidate or "." in candidate or "_" in candidate
            ):
                filename = candidate
                break
            # If the first non-empty line back doesn't look like a path, stop
            # hunting rather than walking further into prose.
            break
        if filename:
            blocks.append((filename, m.group("search"), m.group("replace")))
    return blocks


class ApplyError(RuntimeError):
    """Raised when a SEARCH/REPLACE block can't be applied to the worktree."""


def apply_search_replace_blocks(
    ws: GitWorkspace, blocks: list[tuple[str, str, str]]
) -> tuple[list[str], list[str]]:
    """Apply each block in order to the worktree.

    Returns ``(modified_files, warnings)``. The warnings list is non-fatal
    feedback the runner should surface in its log — typically "the model
    asked to create a file that already exists; treated as overwrite". This
    keeps tasks moving when the model didn't see the file's current state
    (a common failure mode for files not picked by the relevance heuristic).

    Rules:
      - Empty SEARCH on a non-existing path ⇒ create the file.
      - Empty SEARCH on an EXISTING path ⇒ full-file overwrite + warning.
        The diff git emits will show exactly what changed, so the user has
        a complete audit trail.
      - Empty REPLACE with non-empty SEARCH that matches the entire file
        contents ⇒ delete the file.
      - Empty REPLACE in general ⇒ replace the matched span with nothing.
      - Otherwise: SEARCH must appear exactly once in the file. Multiple or
        zero matches raise ApplyError so the runner can report it cleanly.
    """
    assert ws.path is not None
    modified: list[str] = []
    warnings: list[str] = []
    for filename, search, replace in blocks:
        rel = filename.lstrip("./")
        path = ws.path / rel
        if not search.strip():
            if path.exists():
                # Model thought it was creating; the file was already there.
                # Treat as a full-file overwrite and warn loudly.
                warnings.append(
                    f"{rel}: model emitted empty SEARCH (intended create) but "
                    f"the file already existed — applied as full overwrite"
                )
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(replace, encoding="utf-8")
            modified.append(rel)
            continue

        if not path.exists():
            raise ApplyError(f"file does not exist: {rel}")

        current = path.read_text(encoding="utf-8")
        count = current.count(search)
        if count == 0:
            # Try a whitespace-tolerant match before giving up.
            stripped_current = "\n".join(line.rstrip() for line in current.splitlines())
            stripped_search = "\n".join(line.rstrip() for line in search.splitlines())
            if stripped_current.count(stripped_search) == 1:
                current = stripped_current
                search = stripped_search
                count = 1
            else:
                raise ApplyError(
                    f"SEARCH not found in {rel!r} (model's idea of the file "
                    f"diverges from reality)"
                )
        if count > 1:
            raise ApplyError(
                f"SEARCH matches {count} times in {rel!r}; widen the SEARCH "
                f"block to include more context so the match is unique"
            )

        # Whole-file delete: SEARCH was the entire file, REPLACE is empty.
        if not replace.strip() and current.strip() == search.strip():
            path.unlink()
            modified.append(rel)
            continue

        path.write_text(current.replace(search, replace, 1), encoding="utf-8")
        modified.append(rel)
    return modified, warnings


# ── Relevant-file selection for the user prompt ──────────────────────


# Filenames or filename patterns that almost any code task touches. These get
# auto-included whether or not the task prompt mentions them by name, so the
# model always sees the project's current configuration and won't "create"
# a config file that already exists.
_ALWAYS_INCLUDE_BASENAMES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "tsconfig.json",
    "jsconfig.json",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "setup.cfg",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "Makefile",
    "README.md",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.json",
    ".prettierrc",
    ".gitignore",
    "Dockerfile",
    "docker-compose.yml",
}

# Substrings/suffixes that mark a file as configuration. Filenames whose
# basename ends with `.config.js`, `.config.ts`, etc. count as config.
_CONFIG_SUFFIXES = (".config.js", ".config.ts", ".config.cjs", ".config.mjs",
                    ".config.json", ".config.yaml", ".config.yml")


def _is_config_file(path: str) -> bool:
    import os as _os
    base = _os.path.basename(path)
    if base in _ALWAYS_INCLUDE_BASENAMES:
        return True
    return any(base.endswith(suffix) for suffix in _CONFIG_SUFFIXES)


# Words that show up in every prompt without indicating which files are
# involved. Pulled out so we don't match a file just because it has "use" or
# "add" in its name.
_PROMPT_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "out",
    "your", "you", "are", "but", "any", "all", "can", "has", "have", "had",
    "use", "uses", "used", "make", "made", "set", "get", "got", "add",
    "added", "adding", "should", "would", "could", "will", "let", "these",
    "those", "their", "there", "what", "when", "where", "which", "who",
    "whose", "why", "how", "new", "old", "now", "also", "just", "only",
    "some", "more", "less", "than", "then", "very", "such", "each", "both",
    "task", "code", "file", "files", "test", "tests", "project", "function",
    "method", "class", "feature", "implement", "implementation", "support",
    "create", "delete", "update", "modify", "change", "changes", "remove",
    "ensure", "make", "see", "check", "way", "ways", "must", "need", "needs",
    "may", "might", "good", "bad", "yes", "not", "be", "is", "as", "if",
    "or", "to", "in", "on", "by", "of", "at", "an", "a",
}


def _prompt_keywords(prompt: str) -> set[str]:
    """Pull substantive identifiers out of a prompt for filename matching.

    We tokenize on word boundaries, lower-case, and drop the stopwords above.
    The minimum length of 3 chars rules out noise like 'js' or 'py' that
    would otherwise match half the tree.
    """
    import re as _re
    tokens = _re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", prompt or "")
    return {t.lower() for t in tokens if t.lower() not in _PROMPT_STOPWORDS}


def relevant_files_for_prompt(
    prompt: str, all_files: list[str], *, max_files: int = 8
) -> list[str]:
    """Pick files most likely to be touched by a task, by lightweight matching.

    Strategy:
      1. Always include obvious configuration files (vue.config.js,
         package.json, tsconfig.json, pyproject.toml, …) that exist in the
         repo, since every code task tends to be sensitive to them.
      2. Add filenames mentioned literally in the task prompt (full path or
         basename).
      3. Fill the remaining slots with top-level project files (likely entry
         points), excluding tests and hidden paths.

    Capped at `max_files`. The caller is expected to also cap the per-file
    byte size when stitching contents into the prompt.
    """
    import os as _os

    config_files = [f for f in all_files if _is_config_file(f)]

    mentioned: list[str] = []
    for f in all_files:
        if f in config_files:
            continue
        base = _os.path.basename(f)
        if len(base) < 3:
            continue
        if f in prompt or base in prompt:
            mentioned.append(f)

    # Keyword-based matching: any file whose basename (lower-cased) contains
    # a substantive word from the prompt. Catches the "task asks for an
    # income tracker for the Critter app, so load CritterIncome.vue" case
    # where the file isn't named verbatim in the prompt.
    keywords = _prompt_keywords(prompt)
    keyword_matched: list[str] = []
    if keywords:
        for f in all_files:
            if f in config_files or f in mentioned:
                continue
            base_lower = _os.path.basename(f).lower()
            if any(kw in base_lower for kw in keywords):
                keyword_matched.append(f)

    # For small repos every file fits in the prompt; skip the depth/test
    # filter so deeply-nested files (e.g. frontend/src/components/Foo.vue)
    # aren't silently omitted. For large repos, keep the top-level-only rule
    # to avoid flooding the context with unrelated code.
    small_repo = len(all_files) <= max_files * 6
    interesting = [
        f for f in all_files
        if f not in config_files
        and f not in mentioned
        and f not in keyword_matched
        and not f.startswith(".")
        and (small_repo or (
            f.count("/") <= 1
            and not any(p in f.lower() for p in ("test_", "_test.", "/tests/", "/test/"))
        ))
    ]

    # Order: configs first (always-on awareness), then explicit mentions,
    # then keyword-matched files, then fillers. Dedupe preserves order.
    ordered = list(dict.fromkeys(config_files + mentioned + keyword_matched + interesting))
    return ordered[:max_files]


def render_file_contents(
    ws: GitWorkspace, files: list[str], *, max_bytes_per_file: int = 24000
) -> str:
    """Format selected file contents as a section the model can refer to.

    Each file is wrapped in markers so the model knows precisely what it's
    looking at and how to refer back to it in SEARCH/REPLACE blocks.
    """
    parts: list[str] = []
    for f in files:
        try:
            content = ws.read(f)
        except Exception:
            continue
        if len(content) > max_bytes_per_file:
            content = content[:max_bytes_per_file] + f"\n…[truncated; file is {len(content)} bytes]\n"
        parts.append(f"--- FILE: {f} ---\n{content}\n--- END FILE: {f} ---\n")
    return "\n".join(parts)


async def with_workspace(project: dict[str, Any]):
    """Context manager wrapping GitWorkspace."""
    return GitWorkspace(
        github_owner=project["github_owner"],
        github_repo=project["github_repo"],
        default_branch=project.get("default_branch", "main"),
    )


async def request_sr_retry(
    runner: "BaseRunner",
    original_user_prompt: str,
    original_response: str,
    failed_files: list[str],
    error_message: str,
    ws: GitWorkspace,
) -> str:
    """Re-prompt the model with the *actual* contents of the file(s) it tried
    to edit, plus a "your SEARCH didn't match — try again" instruction.

    Returns the new response text (possibly empty if the call fails). The
    caller is responsible for re-parsing SR blocks and re-attempting apply.

    Why this exists: the most common SR-apply failure is the model writing
    a SEARCH block against a file it only saw listed in the tree, not in
    loaded content. Once we hand it the real bytes, it nearly always
    produces a correct block on the second try.
    """
    contents = render_file_contents(ws, failed_files, max_bytes_per_file=20000)
    if not contents.strip():
        return ""  # nothing to retry with — give up cleanly

    retry_user_msg = (
        f"Your SEARCH/REPLACE blocks could not be applied:\n\n"
        f"  {error_message}\n\n"
        f"This usually means the SEARCH text doesn't match the file byte-for-byte. "
        f"Below is the ACTUAL current content of the file(s) you targeted. "
        f"Copy from this verbatim — do not paraphrase — when you write your "
        f"new SEARCH blocks. Indentation, blank lines, and punctuation all "
        f"have to match exactly.\n\n"
        f"{contents}\n\n"
        f"Now re-emit your SEARCH/REPLACE blocks using the real content above. "
        f"Same format and rules as before."
    )
    messages = [
        {"role": "system", "content": runner.system_prompt()},
        {"role": "user", "content": original_user_prompt},
        {"role": "assistant", "content": original_response},
        {"role": "user", "content": retry_user_msg},
    ]
    try:
        result = await runner.provider.chat(runner.model, messages)
    except Exception as exc:
        log.warning("runner.retry_failed", error=str(exc))
        return ""
    msg = result.get("message") or {}
    return msg.get("content") or ""


# Reusable SR-format instruction snippet for runners that edit files.
SEARCH_REPLACE_INSTRUCTIONS = """\
When you need to change files, output one or more SEARCH/REPLACE blocks in
this exact format (a brief plain-prose plan before them is fine; nothing else
matters):

    path/to/file.py
    <<<<<<< SEARCH
    exact lines currently in the file
    =======
    new lines to replace them with
    >>>>>>> REPLACE

Rules:
  - The SEARCH text must match the file's current contents EXACTLY, byte for
    byte, including indentation. Copy from the file shown above; don't
    paraphrase.
  - To create a new file, leave SEARCH empty (just the `<<<<<<< SEARCH`
    marker immediately followed by the `=======` marker).
  - To delete a file, put the entire current file contents in SEARCH and
    leave REPLACE empty.
  - Use one SEARCH/REPLACE block per logical edit. Multiple blocks targeting
    the same file are fine; they apply in order.
  - Do NOT output a unified diff or anything resembling one. SEARCH/REPLACE
    is the only accepted change format.
"""

