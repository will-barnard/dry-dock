#!/usr/bin/env python3
"""dry-dock eval harness.

    python3 run_evals.py --validate-fixtures
    python3 run_evals.py --model qwen2.5-coder:32b
    python3 run_evals.py --model qwen2.5-coder:32b --model qwen2.5-coder:14b --repeat 3

Run it from worker/evals/ with the worker's dependencies importable (the
worker container has them; locally, a venv with worker/requirements.txt).

Why this exists before the Phase 2 rewrite and not after: without a baseline
there is no way to tell whether a change to the prompt, the model, or the
architecture helped. Every number below is one you can re-measure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))            # checks, harness
sys.path.insert(0, str(HERE.parent))     # the `app` package


def _bootstrap_env(args) -> None:
    """Populate the env Settings requires, before app.config is imported.

    Settings has no defaults for the worker identity fields, and its defaults
    for OLLAMA_BASE_URL / WORKTREE_ROOT assume the Docker container. Running
    natively on a Mac needs both overridden.
    """
    defaults = {
        "ORCHESTRATOR_URL": "ws://localhost/none",
        "WORKER_SHARED_SECRET": "evals",
        "WORKER_NAME": "evals",
        "WORKER_POOL": "coder",
        "OLLAMA_BASE_URL": args.ollama,
        "WORKTREE_ROOT": tempfile.mkdtemp(prefix="drydock-eval-worktrees-"),
        "GITHUB_TOKEN": "",
        "GITHUB_USERNAME": "",
    }
    for k, v in defaults.items():
        os.environ.setdefault(k, v)
    # Explicit knobs, so a context sweep is a flag rather than an edit.
    os.environ["MAX_CONTEXT"] = str(args.max_context)
    if args.temperature is not None:
        os.environ["TEMPERATURE"] = str(args.temperature)


def validate_fixtures() -> int:
    """Check that each fixture's assertions FAIL on the untouched seed repo.

    An assertion that already passes before the model runs measures nothing —
    it silently inflates every score that follows. This is the harness testing
    itself, and it is worth running whenever a fixture is added.
    """
    from harness import build_fixture_repo, load_fixtures
    from checks import run_assertion

    problems = 0
    for fx in load_fixtures():
        with tempfile.TemporaryDirectory(prefix="drydock-eval-") as td:
            _bare, reference = build_fixture_repo(fx, Path(td))
            specs = fx.get("assertions") or []
            task_specs = [s for s in specs if not s.get("guard")]
            guard_specs = [s for s in specs if s.get("guard")]
            leaky = [s for s in task_specs if run_assertion(reference, s).ok]
            dead_guards = [s for s in guard_specs if not run_assertion(reference, s).ok]

        if not task_specs:
            print(f"  {fx['id']:22} NO TASK ASSERTIONS — cannot be scored")
            problems += 1
            continue
        if leaky:
            # A task assertion true before the model runs measures nothing and
            # silently inflates every score after it.
            print(f"  {fx['id']:22} BAD: {len(leaky)} task assertion(s) already "
                  f"true on the seed")
            for spec in leaky:
                print(f"      - {spec.get('kind')} {spec.get('path')} {spec.get('pattern','')}")
            problems += 1
        elif dead_guards:
            # A guard that fails on the seed is not guarding anything.
            print(f"  {fx['id']:22} BAD: {len(dead_guards)} guard(s) do not hold "
                  f"on the seed")
            problems += 1
        else:
            print(f"  {fx['id']:22} OK — {len(task_specs)} task assertion(s) fail "
                  f"on the seed, {len(guard_specs)} guard(s) hold")
    return problems


def _row(r) -> str:
    def mark(b, ch="Y"):
        return ch if b else "."
    return (
        f"  {r.fixture:22} {mark(r.green,'GREEN') if r.green else '     .':6} "
        f"patch:{mark(r.produced_patch)} applies:{mark(r.patch_applies)} "
        f"parse:{mark(r.parse_ok)} "
        f"assert:{r.assertions_passed}/{r.assertions_total} "
        f"guard:{r.guards_passed}/{r.guards_total} "
        f"retries:{r.retries} prompt:{r.prompt_tokens or '?'} "
        f"{r.seconds:6.1f}s {r.tokens_per_sec():5.1f}tok/s"
        + ("  OVER-BUDGET" if r.over_budget else "")
        + ("  DROPPED-FILES" if r.dropped_files else "")
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description="dry-dock eval harness")
    ap.add_argument("--model", action="append", default=[],
                    help="model tag; repeat to compare several")
    ap.add_argument("--runner", default="coder", choices=["coder", "engineer"])
    ap.add_argument("--fixture", action="append", default=[],
                    help="fixture id; repeat. Default: all")
    ap.add_argument("--repeat", type=int, default=1,
                    help="runs per fixture. >1 exposes run-to-run variance")
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--max-context", type=int, default=32768)
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--out", default="results.jsonl")
    ap.add_argument("--verbose", action="store_true", help="stream runner logs")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--validate-fixtures", action="store_true",
                    help="check assertions fail on the untouched seed, then exit")
    args = ap.parse_args()

    _bootstrap_env(args)
    from harness import load_fixtures, run_fixture

    if args.list:
        for fx in load_fixtures():
            print(f"{fx['id']:22} {fx.get('difficulty',''):7} {fx.get('title','')}")
        return 0

    if args.validate_fixtures:
        print("\nvalidating fixtures against the untouched seed repo:\n")
        return 0 if validate_fixtures() == 0 else 1

    models = args.model or ["qwen2.5-coder:32b"]
    fixtures = load_fixtures(args.fixture or None)
    if not fixtures:
        print("no fixtures matched", file=sys.stderr)
        return 1

    out_path = Path(args.out)
    results = []
    with out_path.open("w", encoding="utf-8") as fh:
        for model in models:
            print(f"\n=== {model} · runner={args.runner} · "
                  f"num_ctx={args.max_context} ===")
            for fx in fixtures:
                for attempt in range(args.repeat):
                    label = f"{fx['id']}" + (f" #{attempt+1}" if args.repeat > 1 else "")
                    print(f"  running {label} …", flush=True)
                    with tempfile.TemporaryDirectory(prefix="drydock-eval-") as td:
                        r = await run_fixture(
                            fx, model=model, runner_name=args.runner,
                            workdir=Path(td), verbose=args.verbose,
                        )
                    results.append(r)
                    fh.write(json.dumps(asdict(r)) + "\n")
                    fh.flush()
                    print("\033[F\033[K" + _row(r))

    # ── summary ─────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    for model in models:
        rs = [r for r in results if r.model == model]
        if not rs:
            continue
        n = len(rs)
        def pct(f): return f"{100 * sum(1 for r in rs if f(r)) / n:5.1f}%"
        tps = [r.tokens_per_sec() for r in rs if r.seconds > 0]
        assertion_rate = (
            sum(r.assertions_passed for r in rs)
            / max(1, sum(r.assertions_total for r in rs))
        )
        print(f"\n{model}  ({n} runs)")
        print(f"  green (applies + parses + all assertions)  {pct(lambda r: r.green)}")
        print(f"  produced a patch                           {pct(lambda r: r.produced_patch)}")
        print(f"  patch applied cleanly                      {pct(lambda r: r.patch_applies)}")
        print(f"  tier-0 parse clean                         {pct(lambda r: r.parse_ok)}")
        guard_rate = (
            sum(r.guards_passed for r in rs) / max(1, sum(r.guards_total for r in rs))
        )
        print(f"  task assertions passed                     {100*assertion_rate:5.1f}%")
        print(f"  regression guards held                     {100*guard_rate:5.1f}%")
        print(f"  needed a SEARCH/REPLACE retry              {pct(lambda r: r.retries > 0)}")
        print(f"  prompt exceeded budget                     {pct(lambda r: r.over_budget)}")
        print(f"  target files dropped from prompt           {pct(lambda r: r.dropped_files)}")
        if tps:
            print(f"  median tok/s                               {statistics.median(tps):5.1f}")
        print(f"  median seconds/task                        "
              f"{statistics.median([r.seconds for r in rs]):5.1f}")

    print(f"\nrows written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
