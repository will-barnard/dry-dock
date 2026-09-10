"""Tier-0 checks: does the code the model produced even parse?

These are the cheapest gate in the verification ladder (docs/ENGINEER-REBUILD.md
section 03). They are deliberately pure-Python where possible so the harness
runs offline with no toolchain, and they are deliberately shallow: the job is
to catch the failures that dominate first drafts — an unclosed tag, two
<script setup> blocks, malformed JSON — not to replace a compiler.

Every check returns a CheckResult with one of three verdicts. `SKIPPED` is
load-bearing: a check that cannot run is NOT a check that failed. Conflating
those is exactly how a greenfield repo ends up burning every retry attempt on
a build error it was never possible to avoid.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped"


@dataclass
class CheckResult:
    verdict: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        # SKIPPED counts as "not a failure" — see module docstring.
        return self.verdict in (PASS, SKIPPED)


# ── Vue SFC ─────────────────────────────────────────────────────────

# Top-level block openers in a .vue file. Anchored at column 0 because nested
# occurrences inside a template are content, not structure.
_BLOCK_RE = re.compile(r"^<(template|script|style)(\s[^>]*)?>", re.M)
_BLOCK_CLOSE_RE = re.compile(r"^</(template|script|style)>", re.M)

# Void elements never need a closing tag.
_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w.-]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*?)(/?)>")


def check_vue_sfc(source: str) -> CheckResult:
    """Structural sanity check on a single-file component.

    Catches, in rough order of how often models get them wrong:
      - no <template> at all, or more than one
      - a block that is opened and never closed
      - two <script> blocks of the same kind (the classic 'script setup' plus
        'script' duplication when a model rewrites a file it half-remembers)
      - unbalanced or mis-nested tags inside the template
    """
    opens = [(m.start(), m.group(1), (m.group(2) or "")) for m in _BLOCK_RE.finditer(source)]
    closes = [m.group(1) for m in _BLOCK_CLOSE_RE.finditer(source)]

    templates = [o for o in opens if o[1] == "template"]
    if not templates:
        # A render-function or JSX component is legal without a template, but
        # only if there is a script to hold it.
        if not any(o[1] == "script" for o in opens):
            return CheckResult(FAIL, "no <template> and no <script> block")
    if len(templates) > 1:
        return CheckResult(FAIL, f"{len(templates)} top-level <template> blocks (max 1)")

    for kind in ("template", "script", "style"):
        n_open = sum(1 for o in opens if o[1] == kind)
        n_close = sum(1 for c in closes if c == kind)
        if n_open != n_close:
            return CheckResult(
                FAIL, f"<{kind}> opened {n_open}x but closed {n_close}x"
            )

    scripts = [o for o in opens if o[1] == "script"]
    setup = [o for o in scripts if "setup" in o[2]]
    plain = [o for o in scripts if "setup" not in o[2]]
    if len(setup) > 1:
        return CheckResult(FAIL, f"{len(setup)} <script setup> blocks (max 1)")
    if len(plain) > 1:
        return CheckResult(FAIL, f"{len(plain)} plain <script> blocks (max 1)")

    if templates:
        start = templates[0][0]
        end = source.find("\n</template>", start)
        if end == -1:
            return CheckResult(FAIL, "<template> is never closed at top level")
        inner = source[source.find(">", start) + 1: end]
        balanced = _check_tag_balance(inner)
        if balanced:
            return CheckResult(FAIL, f"template: {balanced}")

    return CheckResult(PASS)


def _check_tag_balance(html: str) -> str:
    """Return an error string, or '' when tags nest correctly.

    Ignores void elements and self-closing tags. Not an HTML parser — it will
    not catch everything — but it reliably catches the dropped closing tag,
    which is the failure that costs a whole build cycle to discover.
    """
    stack: list[str] = []
    # Strip comments so <!-- <div> --> doesn't count.
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    for m in _TAG_RE.finditer(html):
        closing, name, _attrs, selfclose = m.groups()
        low = name.lower()
        if low in _VOID or selfclose:
            continue
        if closing:
            if not stack:
                return f"closing </{name}> with nothing open"
            if stack[-1] != low:
                return f"closing </{name}> but innermost open tag is <{stack[-1]}>"
            stack.pop()
        else:
            stack.append(low)
    if stack:
        return f"unclosed tag(s): {', '.join('<' + t + '>' for t in reversed(stack))}"
    return ""


# ── other languages ─────────────────────────────────────────────────


def check_json(source: str) -> CheckResult:
    try:
        json.loads(source)
    except json.JSONDecodeError as exc:
        return CheckResult(FAIL, f"line {exc.lineno}: {exc.msg}")
    return CheckResult(PASS)


def check_python(source: str) -> CheckResult:
    try:
        ast.parse(source)
    except SyntaxError as exc:
        return CheckResult(FAIL, f"line {exc.lineno}: {exc.msg}")
    return CheckResult(PASS)


def check_js_like(source: str, suffix: str) -> CheckResult:
    """Best-effort syntax check for js/ts via esbuild, then node.

    Returns SKIPPED rather than PASS when no tool is available, so a machine
    without a toolchain cannot silently inflate the score.
    """
    if shutil.which("npx"):
        loader = {"ts": "ts", "tsx": "tsx", "jsx": "jsx"}.get(suffix.lstrip("."), "js")
        try:
            proc = subprocess.run(
                ["npx", "--no-install", "esbuild", f"--loader={loader}"],
                input=source, capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0:
                return CheckResult(PASS)
            if "could not determine executable" not in proc.stderr.lower():
                return CheckResult(FAIL, proc.stderr.strip().splitlines()[0][:200])
        except (subprocess.TimeoutExpired, OSError):
            pass
    return CheckResult(SKIPPED, "no esbuild available")


_CHECKERS = {
    ".vue": lambda src, sfx: check_vue_sfc(src),
    ".json": lambda src, sfx: check_json(src),
    ".py": lambda src, sfx: check_python(src),
    ".js": check_js_like,
    ".mjs": check_js_like,
    ".cjs": check_js_like,
    ".ts": check_js_like,
    ".tsx": check_js_like,
    ".jsx": check_js_like,
}


def check_file(path: Path) -> CheckResult:
    """Run the tier-0 check appropriate to this file's extension."""
    suffix = path.suffix.lower()
    fn = _CHECKERS.get(suffix)
    if fn is None:
        return CheckResult(SKIPPED, f"no tier-0 checker for {suffix or 'extensionless'}")
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return CheckResult(FAIL, f"unreadable: {exc}")
    return fn(source, suffix)


# ── fixture assertions ──────────────────────────────────────────────


def run_assertion(repo: Path, spec: dict) -> CheckResult:
    """Evaluate one declarative assertion from a fixture.

    Supported kinds:
      file_exists      {path}
      file_absent      {path}
      contains         {path, pattern}     regex, searched multiline
      not_contains     {path, pattern}
    """
    kind = spec.get("kind")
    rel = spec.get("path", "")
    target = repo / rel

    if kind == "file_exists":
        return CheckResult(PASS) if target.is_file() else CheckResult(FAIL, f"{rel} missing")
    if kind == "file_absent":
        return CheckResult(PASS) if not target.exists() else CheckResult(FAIL, f"{rel} exists")

    if kind in ("contains", "not_contains"):
        if not target.is_file():
            return CheckResult(FAIL, f"{rel} missing")
        body = target.read_text(encoding="utf-8", errors="replace")
        hit = re.search(spec.get("pattern", ""), body, re.M | re.S) is not None
        want = kind == "contains"
        if hit == want:
            return CheckResult(PASS)
        verb = "does not contain" if want else "unexpectedly contains"
        return CheckResult(FAIL, f"{rel} {verb} /{spec.get('pattern','')}/")

    return CheckResult(SKIPPED, f"unknown assertion kind: {kind!r}")
