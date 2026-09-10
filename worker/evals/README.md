# dry-dock eval harness

Measures whether a runner can actually do a coding task, offline and
reproducibly. Built before the Phase 2 engineer rewrite on purpose: without a
baseline there is no way to tell whether a change to the prompt, the model, or
the architecture helped.

See `docs/ENGINEER-REBUILD.md` section 06, Phase 1.

## Running it

Needs the worker's dependencies importable and an Ollama to talk to.

```bash
# inside the worker container (deps already present)
docker compose exec worker python3 /app/evals/run_evals.py \
    --model qwen2.5-coder:32b --ollama http://host.docker.internal:11434

# or natively on the Mac, in a venv with worker/requirements.txt
cd worker/evals
python3 run_evals.py --model qwen2.5-coder:32b
```

Useful flags:

| Flag | Purpose |
|---|---|
| `--model` | repeatable — compare models on identical fixtures |
| `--runner` | `coder` today, `engineer` once Phase 2 lands. This is how you race them. |
| `--fixture` | repeatable — run one task while iterating on a prompt |
| `--repeat N` | N runs per fixture; local models vary run to run and one sample lies |
| `--max-context` | sweep `num_ctx` to find where quality stops improving |
| `--verbose` | stream the runner's logs |
| `--validate-fixtures` | check the harness itself (see below) |

Results stream to `results.jsonl`, one row per run.

## What it measures

Per run, in the order a task has to survive:

| Metric | Meaning |
|---|---|
| **produced a patch** | the model emitted edits that parsed as SEARCH/REPLACE and applied to its own worktree |
| **patch applies** | `git apply` accepts the diff against a clean checkout — what the orchestrator would do |
| **tier-0 parse** | every changed file parses (SFC structure, JSON, TS/JS via esbuild when available) |
| **task assertions** | fixture-specific checks that the change does what was asked |
| **regression guards** | that existing content survived the edit |
| **green** | all of the above. The only number that matters. |
| **retries** | SEARCH/REPLACE apply failures that needed a re-prompt |
| **prompt tokens / over-budget / dropped files** | telemetry from the Phase 0 budgeting — how close to the window a task runs, and whether target files were silently omitted |
| **tok/s, seconds** | cost. Matters more for the engineer loop, which makes many calls per task. |

`green` is deliberately strict: a patch that does not apply is worth nothing
no matter how good the prose was.

## Validating the harness

```bash
python3 run_evals.py --validate-fixtures
```

Every task assertion must FAIL on the untouched seed repo. An assertion that
already passes measures nothing and silently inflates every score after it —
this catches that. Regression guards are the deliberate exception: they hold
on the seed by design, so they are checked the other way (a guard that fails
on the seed is not guarding anything). Run this whenever you add a fixture.

`python3 check_selftest.py` tests the tier-0 checkers themselves.

## Adding a fixture

Drop a JSON file in `fixtures/tasks/`:

```json
{
  "id": "07-something",
  "title": "Short description",
  "difficulty": "easy|medium|hard",
  "prompt": "What the agent is told. Write it the way your planner would.",
  "payload": { "target_files": ["src/components/Thing.vue"] },
  "seed_files": { "src/Broken.vue": "…contents…" },
  "delete_files": ["src/Unwanted.vue"],
  "assertions": [
    { "kind": "file_exists", "path": "src/components/Thing.vue" },
    { "kind": "contains", "path": "src/components/Thing.vue", "pattern": "defineProps" },
    { "kind": "not_contains", "path": "src/components/Thing.vue", "pattern": ": any" },
    { "kind": "contains", "path": "src/App.vue", "pattern": "existing", "guard": true }
  ]
}
```

`seed_files` writes into the fixture's repo before the runner sees it — that is
how `04-fix-type-error` stages a deliberate bug. Patterns are regexes.

Then run `--validate-fixtures`.

## How it works

Each fixture gets a throwaway local git repo built from `fixtures/seed-repo`
plus its own overrides, committed and cloned bare. `CLONE_URL_OVERRIDE` points
the runner at that `file://` URL, so there is no network, no GitHub, and no
shared state between fixtures. The runner is invoked in-process with a
hand-built `RunnerContext` — no orchestrator, no database, no dispatch — so a
failure is the runner's, not the plumbing's.

Scoring reads the emitted **patch artifact**, not the worktree: runners delete
their worktree in a `finally`, and the patch is the real deliverable anyway.

## Known limits

- **No `npm run build`.** Assertions are structural, so the harness runs in
  seconds with no `node_modules`. That means it measures "plausibly correct
  code" rather than "compiles". Tier 2 belongs here once Phase 3 lands
  `run_check`, and the seed repo is a real Vite project so it can.
- **Regex assertions accept wrong-but-matching code.** They catch the failures
  that dominate today (no output, unparseable output, missing the point). They
  will need to get stricter as the runner gets better.
- **Six fixtures is a small sample.** Use `--repeat 3` or more before trusting
  a difference between two models.
