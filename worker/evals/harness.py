"""Drive one runner against one fixture, offline, and score what comes out.

Design notes worth knowing before you change anything here:

* **No orchestrator, no database, no worker process.** The runner is invoked
  in-process with a hand-built RunnerContext. That makes a run reproducible
  and fast, and it means a failure is the runner's failure rather than a
  dispatch or routing artifact.

* **Every fixture gets its own throwaway git repo.** The seed tree plus any
  per-fixture overrides are committed to a local bare repo, and
  CLONE_URL_OVERRIDE points the runner at it. No GitHub, no network, no
  shared mutable state between fixtures.

* **Scoring reads the patch artifact, not the worktree.** Runners delete their
  worktree in a `finally`, so there is nothing to inspect afterwards — but
  more importantly the patch IS the deliverable. It is what the orchestrator
  would apply and push. If it does not apply cleanly to a fresh checkout, the
  task failed no matter how good the prose was.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from checks import FAIL, PASS, SKIPPED, CheckResult, check_file, run_assertion

HERE = Path(__file__).parent
SEED_DIR = HERE / "fixtures" / "seed-repo"
TASKS_DIR = HERE / "fixtures" / "tasks"


# ── result record ───────────────────────────────────────────────────


@dataclass
class FixtureResult:
    fixture: str
    model: str
    runner: str
    # what the runner said
    success: bool = False
    summary: str = ""
    error: str = ""
    # what it actually produced
    produced_patch: bool = False
    patch_applies: bool = False
    files_changed: list = field(default_factory=list)
    # tier 0
    parse_verdicts: dict = field(default_factory=dict)
    parse_ok: bool = False
    # fixture-specific correctness
    assertions_passed: int = 0
    assertions_total: int = 0
    # Regression guards are tracked apart from task assertions: they hold on
    # the untouched seed, so folding them in would pay the model for work it
    # did not do. They still gate `green` — dropping existing content is a
    # real failure — they just do not inflate the assertion rate.
    guards_passed: int = 0
    guards_total: int = 0
    assertion_detail: list = field(default_factory=list)
    # cost + the prompt-budget telemetry Phase 0 added
    seconds: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    over_budget: bool = False
    dropped_files: bool = False

    @property
    def green(self) -> bool:
        """The bar that matters: applied cleanly, parses, and does the job."""
        return (
            self.produced_patch
            and self.patch_applies
            and self.parse_ok
            and self.assertions_total > 0
            and self.assertions_passed == self.assertions_total
            and self.guards_passed == self.guards_total
        )

    def tokens_per_sec(self) -> float:
        return self.tokens_out / self.seconds if self.seconds > 0 else 0.0


# ── git plumbing ────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "drydock-evals",
        "GIT_AUTHOR_EMAIL": "evals@localhost",
        "GIT_COMMITTER_NAME": "drydock-evals",
        "GIT_COMMITTER_EMAIL": "evals@localhost",
    }
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), env=env,
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def build_fixture_repo(fixture: dict, workdir: Path) -> tuple[Path, Path]:
    """Materialize the seed tree + fixture overrides as a local bare repo.

    Returns (bare_repo_path, reference_checkout_path). The reference checkout
    is a pristine copy used later to test that the patch applies.
    """
    seed = workdir / "seed"
    shutil.copytree(SEED_DIR, seed)

    # Per-fixture overrides: seed_files writes/replaces, delete_files removes.
    # This is how a fixture stages a deliberate bug for the model to fix.
    for rel, content in (fixture.get("seed_files") or {}).items():
        target = seed / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for rel in fixture.get("delete_files") or []:
        p = seed / rel
        if p.exists():
            p.unlink()

    _git(["init", "-q"], seed)
    _git(["checkout", "-q", "-b", "main"], seed)
    _git(["add", "-A"], seed)
    _git(["commit", "-q", "-m", "seed"], seed)

    bare = workdir / "repo.git"
    _git(["clone", "-q", "--bare", str(seed), str(bare)], workdir)

    reference = workdir / "reference"
    _git(["clone", "-q", "--branch", "main", str(bare), str(reference)], workdir)
    return bare, reference


# ── running one fixture ─────────────────────────────────────────────


_RETRY_RE = re.compile(r"retrying with actual contents")
_PROMPT_RE = re.compile(r"prompt ~(\d+) tokens")


async def run_fixture(
    fixture: dict, *, model: str, runner_name: str, workdir: Path, verbose: bool = False
) -> FixtureResult:
    # Imported lazily: app.config reads the environment at first call and
    # caches it, so run_evals.py must set env vars before this point.
    from app.config import get_settings
    from app.runners.base import RunnerContext

    result = FixtureResult(fixture=fixture["id"], model=model, runner=runner_name)

    bare, reference = build_fixture_repo(fixture, workdir)
    os.environ["CLONE_URL_OVERRIDE"] = f"file://{bare}"
    get_settings.cache_clear()

    logs: list[tuple[str, str]] = []
    artifacts: list[dict] = []

    async def emit_log(stream: str, body: str) -> None:
        logs.append((stream, body))
        if verbose:
            print(f"      [{stream}] {body.rstrip()[:300]}")

    async def emit_artifact(kind: str, name: str, content: str, metadata: dict) -> None:
        artifacts.append({"kind": kind, "name": name, "content": content, "metadata": metadata})

    ctx = RunnerContext(
        task_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        title=fixture.get("title", fixture["id"]),
        prompt=fixture["prompt"],
        project={
            "slug": "evals",
            "github_owner": "evals",
            "github_repo": "fixture",
            "default_branch": "main",
            "system_prompt": fixture.get("system_prompt") or SEED_CONVENTIONS,
            "validate_commands": [],
        },
        payload=fixture.get("payload") or {},
        preferred_model=model,
        emit_log=emit_log,
        emit_artifact=emit_artifact,
        branch_name=None,
    )

    runner_cls = _runner_class(runner_name)
    started = time.monotonic()
    try:
        rr = await runner_cls(ctx).run()
        result.success = rr.success
        result.summary = (rr.summary or "")[:400]
        result.tokens_in, result.tokens_out = rr.tokens_in, rr.tokens_out
    except Exception as exc:  # a crashed runner is a data point, not a stop
        result.error = f"{type(exc).__name__}: {exc}"
    result.seconds = round(time.monotonic() - started, 2)

    # ── telemetry from the Phase 0 logging ──────────────────────────
    joined = "\n".join(b for _, b in logs)
    result.retries = len(_RETRY_RE.findall(joined))
    m = _PROMPT_RE.search(joined)
    if m:
        result.prompt_tokens = int(m.group(1))
    result.over_budget = "exceeds the" in joined and "input budget" in joined
    result.dropped_files = "did not fit the token budget" in joined

    # ── score the patch ─────────────────────────────────────────────
    patch = next((a for a in artifacts if a["kind"] == "patch"), None)
    if patch and patch["content"].strip():
        result.produced_patch = True
        _score_patch(result, patch["content"], reference, fixture)
    return result


def _score_patch(
    result: FixtureResult, patch: str, reference: Path, fixture: dict
) -> None:
    patch_file = reference.parent / "agent.diff"
    patch_file.write_text(patch, encoding="utf-8")

    applied = _git(["apply", "--whitespace=nowarn", str(patch_file)], reference, check=False)
    if applied.returncode != 0:
        result.patch_applies = False
        result.error = result.error or f"git apply: {applied.stderr.strip()[:300]}"
        return
    result.patch_applies = True

    status = _git(["status", "--porcelain"], reference)
    changed = [ln[3:].strip() for ln in status.stdout.splitlines() if ln.strip()]
    result.files_changed = changed

    # Tier 0 on every file the patch touched.
    verdicts: dict[str, str] = {}
    for rel in changed:
        p = reference / rel
        if not p.is_file():
            continue  # deletion
        res = check_file(p)
        verdicts[rel] = res.verdict
        if res.verdict == FAIL:
            verdicts[rel] = f"fail: {res.detail}"
    result.parse_verdicts = verdicts
    result.parse_ok = all(not v.startswith("fail") for v in verdicts.values())

    # Fixture assertions.
    for spec in fixture.get("assertions") or []:
        res = run_assertion(reference, spec)
        if spec.get("guard"):
            result.guards_total += 1
            result.guards_passed += 1 if res.ok else 0
        else:
            result.assertions_total += 1
            result.assertions_passed += 1 if res.ok else 0
        result.assertion_detail.append(
            {"spec": spec, "verdict": res.verdict, "detail": res.detail,
             "guard": bool(spec.get("guard"))}
        )


def _runner_class(name: str):
    """Map a harness --runner name to a runner class.

    Keeping this explicit (rather than reusing app.runners.RUNNERS) is what
    lets the harness race the current coder against a new engineer runner on
    identical fixtures — the whole point of building this before the rewrite.
    """
    if name == "coder":
        from app.runners.coder import CoderRunner
        return CoderRunner
    if name == "engineer":  # Phase 2
        from app.runners.engineer import EngineerRunner  # noqa
        return EngineerRunner
    raise SystemExit(f"unknown runner {name!r} (expected: coder, engineer)")


# Default project system_prompt for the fixtures. Real projects should set
# their own; this exists so the eval measures the runner, not the absence of
# conventions.
SEED_CONVENTIONS = """\
This project is Vue 3 + Vite + TypeScript.
Conventions, follow them exactly:
- Single-file components using <script setup lang="ts">. Never the Options API.
- Composition API only: ref/computed/watch imported from 'vue'.
- Components live in src/components/ and are named in PascalCase.
- Composables live in src/composables/ and are named useThing.ts.
- Routes are registered in src/router/index.ts.
- Two-space indent, no semicolons, single quotes.
"""


def load_fixtures(only: list[str] | None = None) -> list[dict]:
    out = []
    for path in sorted(TASKS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("id", path.stem)
        if only and data["id"] not in only:
            continue
        out.append(data)
    return out
