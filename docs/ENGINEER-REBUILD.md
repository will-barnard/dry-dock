# Engineer module — review and rebuild plan

Reviewed at commit `4b097f9`. Companion to `ROADMAP.md`; this supersedes the
Phase 2 "Coder" bullets there.

---

## 00 · Start here: every prompt is truncated to 4096 tokens, tail-first

**Severity: critical. One-line fix.**

`BaseRunner.run()` calls `chat_stream(self.model, messages)` with no `options`.
`OllamaProvider.chat_stream` passes `"options": options or {}` — an empty dict.
Ollama therefore uses its **default context window of 4096 tokens**, and when the
prompt exceeds it, it **keeps the tail and drops the head**.

Meanwhile `CoderRunner` builds a prompt containing the full repo file tree plus up
to **20 files x 24,000 bytes** — potentially 480 KB, on the order of 120,000
tokens, against a 4,096-token window. On any real task the model receives roughly
the last 4k tokens and nothing else:

- The system prompt is gone, including all of `SEARCH_REPLACE_INSTRUCTIONS`.
  The model was never told the output format.
- The `## Current contents of the target files` section is gone. The model has
  never seen the file it is being asked to edit.
- The contract is gone. The repo tree is gone.
- What survives is the trailing `## Task` paragraph.

Every reported symptom falls out of this. "No SEARCH/REPLACE blocks in the output"
— it was never told to emit them. "SEARCH doesn't match reality" — it never saw
reality. "Takes many iterations" — each retry re-truncates identically. The retry
helpers make it worse: `request_sr_retry` appends file contents *and* the previous
response to an already-oversized prompt.

```
# worker/app/runners/base.py

-    async for ev in self.provider.chat_stream(self.model, messages):
+    async for ev in self.provider.chat_stream(
+        self.model, messages, options=self.inference_options()
+    ):

# on BaseRunner:
def inference_options(self) -> dict:
    s = get_settings()
    return {
        "num_ctx":        s.max_context,   # already configured, never used
        "temperature":    0.15,            # Ollama's 0.8 default is wrong for code
        "top_p":          0.9,
        "repeat_penalty": 1.05,
        "num_predict":    8192,
    }
```

Evidence: `grep -rn "num_ctx" worker/ backend/` returns nothing. `MAX_CONTEXT`
exists at `worker/app/config.py:19` but is only *advertised* to the orchestrator
for routing (`router.py:48`); it never reaches inference.

Note: setting `num_ctx` alone makes the failure loud rather than silent. A 120k
prompt against a 32k window still truncates. See finding 03 (budget) and 02
(remove the need for the giant prompt).

---

## 01 · Remaining findings, ranked by leverage

### 02 — One LLM call per task, with no ability to look at anything (critical)

`BaseRunner.run()` is: build messages, stream once, parse. The coder gets one
shot. It cannot open a file it wasn't handed, check whether an import resolves,
run the compiler, or see its own output. A Vue SFC touches the component, its
store, its types, its router entry, and its siblings' conventions.

The machinery for the fix already exists here: `OllamaProvider.chat` accepts
`tools`, and `orchestrator/chat.py` + `tools.py` already implement a tool-calling
loop for the Operator.

Refs: `worker/app/runners/base.py:79-124`, `worker/app/runners/coder.py`

### 03 — Build-error feedback is minutes long and discards all state (critical)

When `npm run build` fails: validator task fails -> result crosses the WS ->
orchestrator appends stderr to the parent's **prompt string** -> parent re-queued
-> dispatcher waits for a worker -> **fresh shallow clone** -> model reloads ->
regenerates **from scratch** with no memory of what it wrote.

The model never sees its broken code and the compiler error together.
`max_attempts` is 3, so you get three independent cold guesses.

Ref: `backend/app/routes/workers.py:275-308`

### 04 — Broken code is pushed and PR'd before validation (critical)

In `_handle_result`, a successful coder result means "the patch parsed," not "the
code works." The orchestrator immediately runs `apply_patch_and_push` and
`open_pull_request`. Validation is a separate downstream task on another machine.
Green build should be a **precondition of emitting the patch artifact**.

Ref: `backend/app/routes/workers.py:182-215`

### 05 — The reviewer reviews an empty string (major)

`ReviewerRunner.setup()` reads `payload["diff"]`. Nothing in the codebase ever
writes that key — `materialize_plan` copies only `contract` and `target_files`.
Every review task has run against `""`, logged `"reviewer: no diff in payload"`,
and returned `success=True`.

Fix: have the reviewer clone the branch and run `git diff <base>...<branch>`
itself. Delete the payload channel.

### 06 — File selection is substring matching, and front-loads the lockfile (major)

`relevant_files_for_prompt` matches basenames against >=3-char prompt tokens.
`_ALWAYS_INCLUDE_BASENAMES` puts `package-lock.json` and `pnpm-lock.yaml` *first*
in the ordering — 24 KB of noise in the most valuable prompt position, ahead of
the file being edited.

What a Vue task needs: the target file's real bytes, its resolved imports one hop
out, its route registration, and one existing sibling component as a conventions
exemplar. That's a dependency walk, not a word match.

Refs: `worker/app/runners/base.py:376-401`, `:470-520`

### 07 — Nothing checks that generated code parses (major)

`apply_search_replace_blocks` writes bytes; `ws.diff()` makes a patch. Whether the
result is a valid SFC is discovered minutes later by a full build on another
machine. A `@vue/compiler-sfc` parse costs ~30 ms.

### 08 — SEARCH/REPLACE is the wrong instrument for new files (major)

The rationale for SR over unified diffs is sound, and it's a good format for
surgical edits. But creating a new `.vue` file through an empty SEARCH is ceremony
small models fumble. The brittleness shows in how many escape hatches
`apply_search_replace_blocks` has grown: whitespace-tolerant rematch,
empty-SEARCH-becomes-overwrite, populated-SEARCH-on-missing-file-becomes-create.
Each silently changes the semantics of what the model asked for.

Split them: `write_file(path, content)` for new/rewritten files,
`edit_file(path, search, replace)` for surgical changes, and hard-fail on no
match — in an agent loop a hard failure is just another tool result.

Ref: `worker/app/runners/base.py:245-320`

### 09 — The planner commits to target_files before reading any code (minor)

The planner sees a file tree only. When it guesses wrong, `task_target_files`
filters bad paths out and the coder *silently* falls back to the keyword
heuristic. Log that fallback; better, give the planner `read_file` and `grep`.

Refs: `worker/app/runners/base.py:604-620`, `coder.py:78-90`

### 10 — Model thrash and no per-project conventions (minor)

Ollama's default `keep_alive` is 5 minutes; across a loop with build steps you pay
weight-load repeatedly. Set `OLLAMA_KEEP_ALIVE=-1` and run one heavy model per
machine. `Project.system_prompt` is a free-text field nobody fills in — a short
per-repo conventions block (Vue 3 `<script setup>` vs Options API, TS vs JS, Pinia
vs Vuex, styling system, import alias) is the cheapest quality win available.

---

## 02 · Target architecture: the engineer loop

Replace `CoderRunner` with an `EngineerRunner` that keeps one conversation alive
for the whole task.

```
setup:  clone -> detect stack -> load conventions -> warm deps
seed:   contract + target files + one exemplar   (stable prefix, cache-friendly)

loop (capped by steps / tokens / wall clock):
    model call with tools           # KV cache stays warm
      -> read_file / grep / list_dir
      -> write_file / edit_file     # tier-0 parse before it lands
      -> run_check(name)            # error returns as a tool result, in-context
    until green or budget spent

exit:   full validate in the same worktree
        emit patch ONLY if green; otherwise fail loudly with the log
```

### Tool surface (keep it small — local models degrade as tool count grows)

| Tool | Returns | Notes |
|---|---|---|
| `read_file(path, start?, end?)` | exact bytes with line numbers | line range so a 2,000-line file doesn't blow the window |
| `list_dir(path)` | names, types, sizes | cheaper than dumping the tree in the seed prompt |
| `grep(pattern, glob?)` | matching lines with context | ripgrep in the worktree |
| `write_file(path, content)` | ok, or tier-0 parse error | **rejects on syntax error — the file never lands broken** |
| `edit_file(path, search, replace)` | ok, or "no match" / "N matches" | hard failure is fine — it's just another turn |
| `run_check(name)` | exit code + trimmed output | named checks from the project row, tail-trimmed |
| `finish(summary)` | — | explicit termination beats guessing from prose |

### Loop control

- **Three independent caps:** max steps (~25), cumulative token budget, wall clock
  (~15 min). Whichever trips first ends the task with work-so-far preserved as
  artifacts.
- **No-progress detector:** two consecutive `run_check` calls returning
  byte-identical output means the model is spinning. Inject a "you've tried this
  twice, change approach or finish" turn; stop if it repeats.
- **Context compaction:** at ~70% of `num_ctx`, replace old tool results with
  one-line summaries and keep current file states. Never let the transport
  truncate silently — that's the bug in finding 00.
- **Non-tool-calling fallback:** keep the SR text protocol behind the same
  interface. Detect capability at registration (`installed_models` already flows
  to the orchestrator) and pick protocol per model, not per pool.
- **Stream tool calls to the dashboard** as `Event` rows with their own `kind`, so
  a failed task is legible after the fact.

---

## 03 · The verification ladder

Run gates in cost order. Tiers 0-1 live inside the loop; tier 2+ can stay as the
validator task, but must run **before** the patch ships.

| Tier | Check | Cost | Notes |
|---|---|---|---|
| 0 | Parse | ~30 ms, every write | `@vue/compiler-sfc` for `.vue`, `esbuild`/`node --check` for js/ts, `ast.parse` for py, `json.loads` for json. Failure rejects the write; the parser error is the tool result. |
| 1 | Typecheck touched files | ~2-10 s, on demand | `vue-tsc --noEmit`, `tsc --noEmit`, `ruff check`, scoped `eslint`. Catches "imported a thing that doesn't exist". |
| 2 | Project build | ~20-90 s, before shipping | Existing `validate_commands`. Warm deps once at clone time. **This is the gate that must move.** |
| 3 | Tests | varies | Also where `TesterRunner` becomes honest — today it writes tests and never runs them. |
| 4 | Smoke the running app | ~15 s | **The real answer to "workable apps."** |

Tier 4 detail: this is what separates "compiles" from "works." **You already have
the service** — `renderer/` runs Playwright + Chromium for Scout. Give it a second
endpoint that takes a URL and returns
`{ rendered_text, console_errors, failed_requests }`, and expose it as
`run_check("smoke")`. A Vue app that builds clean but throws
"Cannot read properties of undefined" on mount is currently indistinguishable from
success.

---

## 04 · Models and runtime on 64 GB

An agent loop changes the arithmetic: 10-30 calls per task with a growing
conversation. So (a) you need real context, 32-64k, and KV cache is no longer a
rounding error; (b) tokens/sec matters far more, because latency multiplies by
step count.

Envelope on 64 GB unified: macOS wants 8-10 GB, so plan for ~44-48 GB addressable
for weights plus KV (raise deliberately with `iogpu.wired_limit_mb` for the top of
that range). That points at a **MoE model in the 30-80B-total / ~3B-active class
at Q4** — roughly 35-40 GB of weights, leaving room for a 64k window. MoE is the
right shape specifically because low active-parameter count buys the tokens/sec an
agent loop spends.

| Setting | Do | Why |
|---|---|---|
| Context | `num_ctx` 32768 -> 65536 | Start at 32k; raise if compaction fires often. KV scales linearly. |
| Residency | `OLLAMA_KEEP_ALIVE=-1` | The loop pauses for builds; otherwise you re-load weights mid-task. |
| Concurrency | One heavy worker per Mac | Two 35 GB models on 64 GB swap. Mac mini takes docs/researcher; MacBook takes engineer. |
| Runtime | Ollama's MLX runner first | Ships in current Ollama, ~20% faster, costs nothing. The `InferenceProvider` seam stays available for MLX-LM direct later. |
| Sampling | `temperature` ~0.15 | Ollama's 0.8 default is tuned for chat. |
| Prompt cache | Keep the prefix stable | Contract + conventions + file seed at the front, never rewritten, so each turn reuses the cached prefix. |

### Don't pick the model from a blog post

The local coding leaderboard turns over every few weeks, and published benchmarks
measure single-shot completion, not "can it drive a 20-step tool loop against a
Vue repo without losing the thread." Tool-calling reliability is where candidates
actually differ.

Build a bake-off harness. Freeze ~20 dry-dock-shaped tasks against a pinned repo
snapshot — "add a component," "wire a route," "fix this type error," "add a field
end-to-end." Per candidate model record:

- **Tier-0 pass rate** — % of writes that parse first try
- **Green rate** — % of tasks reaching a passing build
- **Steps to green** — median loop iterations (the convergence metric)
- **Tool-call validity** — % of calls with well-formed args naming real paths
- **Tokens/sec and peak memory** — at your actual `num_ctx`

Build it *before* the agent rewrite. It's the only way to know whether any change
here helped, and it doubles as a regression suite for prompt changes.

---

## 05 · Fleet

Three routing faults are live right now. Each silently removes capacity you
already own, and none is visible from the dashboard.

### F1 — The Windows box is receiving zero work (critical)

`reviewer` and `tester` are explicitly pinned to `qwen2.5-coder:32b` in the
settings table. The Windows workers have only `qwen2.5-coder:14b` and `:7b`
installed — and a DB-saved role model is a **hard filter**, not a suggestion
(`get_role_model_if_set` -> `_worker_compatible`).

Both Windows workers therefore fail the capability check, their priority-1 tier
is judged to contain no capable worker, and strict failover drops through to
`macbook-reviewer-1` / `macbook-tester-1` at priority 100. Every review and test
has been running on the MacBook, competing with the coder for the same 64 GB.

**Upgrading the GPU changes nothing until those two dropdowns are set back to
"fall back to env default."**

Refs: `router.py:81-105`, `settings_service.py:87-100`

### F2 — There is no validator worker, so the gate has never run (critical)

`validator` is in `KNOWN_POOLS` and `TaskKind.VALIDATE` maps to it — but it is
missing from the valid-pool list in both `docs/WORKER_SETUP.md:52` and
`worker/docker-compose.example.yml:22`, and it is absent from `KNOWN_ROLES`, so
it never appears on the settings page. Nothing ever told you to start one.

The planner is instructed to emit a `validate` task after every code and refactor
task. Every one has gone to QUEUED and stayed there with no worker to claim it.
The auto-requeue loop at `workers.py:275` — the write/validate/fix cycle — **has
never executed once.** Finding 03 above isn't that the loop is slow; it's that
the loop does not exist.

Verify: `GET /api/workers`, look for `pool=validator`. One env file fixes it.

### F3 — The MacBook is oversubscribed five to one (major)

`worker/envs/` holds five `.env` files — planner, coder, reviewer, tester,
refactorer — all `HARDWARE_CLASS=macbook`, all `RAM_GB=64`, all started together
by `workers.sh up`. Five containers on one 64 GB machine, all pointed at a single
Ollama on `host.docker.internal:11434`, each advertising 64 GB. The router
believes it has 320 GB of Mac capacity.

Two concrete failures follow. Ollama may try to hold more than one model — you
currently dodge this because every Mac role is pinned to the same 32b, but the
moment they differ you swap 20 GB in and out mid-plan. And `OLLAMA_NUM_PARALLEL`
defaults to a multi-slot value with KV allocated as `num_ctx x slots`, so
`num_ctx=32768` can quietly reserve a 131k-token cache.

Five host-level env vars on the Mac make the oversubscription safe:

```
OLLAMA_MAX_LOADED_MODELS=1   # never hold two 20GB models at once
OLLAMA_NUM_PARALLEL=1        # KV = num_ctx, not num_ctx x slots
OLLAMA_KEEP_ALIVE=-1         # weights stay resident across build steps
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0    # halves KV; requires flash attention
```

Requests queue instead of contending. Costs nothing; highest-value change on the
Mac after `num_ctx`.

### Target shape

The engineer loop makes 10-30 calls plus builds per task, so the scarce resource
is the one box that can hold a large model at long context. Guiding rule: **get
everything off the MacBook that doesn't need a big model.** Every review, test,
docs and build cycle that lands there is a cycle the engineer isn't getting.

| Machine | Pools | Why |
|---|---|---|
| **MacBook Pro** (64 GB unified) | engineer (ex-coder), planner, refactorer | Only box that holds a 32B-class model at 32-64k. Protect it. Tier 0-1 checks run here, inside the loop. |
| **Windows** (RTX 5080, 16 GB; 64 GB RAM) | reviewer, tester, **validator** | 14b fully resident on 16 GB is fast, and reviewer/tester are short-context by nature. The validator does no inference — CPU, RAM and disk, which this box has spare while the Mac generates. |
| **Mac mini** (24 GB unified) | docs, researcher, second validator | Small models, no contention with the engineer. A second validator gives the build gate some parallelism. |

**On the 5080:** a stock RTX 5080 is 16 GB GDDR7; the 24 GB Super variant slipped
out of 2026. Confirm with `nvidia-smi` before sizing. `qwen2.5-coder:14b` at
Q4_K_M is roughly 9 GB of weights, leaving ~7 GB for KV and driver overhead, so
32k should fit with `q8_0` KV quantization. The check that settles it is
`ollama ps` — it must report **100% GPU**. Any CPU split and you want 24576
instead; a partial offload is dramatically slower than a smaller window. Note
that `:32b` at Q4 is ~20 GB and does not fit — keep 32b work on the Mac.

**One structural limit:** `WORKER_POOL` is a single string, so "one worker
process per machine serving several pools" isn't expressible today — which is why
there are five containers on one Mac. Making it a comma list is small:
`RegisterMsg.pool` -> `pools: list[str]`, `registry.by_pool` matches membership,
dispatcher unchanged. Worth doing when the engineer lands in Phase 2, because
that's when you want one process per Mac holding one warm model. Not urgent today
— the five Ollama env vars buy the same safety.

**Two decorative fields:** `hardware_class` is display-only (nothing routes on
it), and `min_vram_gb` is never set by anything — no planner field, no UI control
— so `GPU_VRAM_GB` does nothing today. Both become load-bearing the moment you
route smoke tests toward a GPU box or builds away from one. Set them accurately
now so routing works the day you switch it on.

---

## 06 · Sequence

### Phase 0 — Stop discarding the prompt (~half a day)

Do this first, independently of everything else.

- `base.py` — add `inference_options()`, pass to both `chat_stream` **and**
  `chat` (the retry helpers call `chat` and are truncated too).
- `config.py` — `MAX_CONTEXT` to 32768; add `TEMPERATURE`, `NUM_PREDICT`.
- `base.py` — hard prompt budget: estimate tokens, and if the assembled prompt
  exceeds ~70% of `num_ctx`, drop files and **log it loudly**.
- Remove `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock` from
  `_ALWAYS_INCLUDE_BASENAMES`.
- Set the five Ollama host vars from section 05 on every worker machine.
- Fill in `Project.system_prompt` for the test repo with real conventions.
- **Fleet F1:** clear the `reviewer` and `tester` role->model overrides so the
  Windows box stops being filtered out.
- **Fleet F2:** start a `validator` worker; add `validator` to `KNOWN_ROLES`,
  `WORKER_SETUP.md:52`, and the compose comment.
- **Fleet F3:** update the Windows env files for the 5080 — `GPU_VRAM_GB=16`,
  `HARDWARE_CLASS=windows-rtx5080`, `MAX_CONTEXT=32768`.
- Fix `GITHUB_TOKEN` on the *Windows* workers — it is a copy of
  `WORKER_SHARED_SECRET` there, so private-repo clones fail. The Mac env files
  already carry a real PAT.
- **Set `validate_commands` on the project.** Without them
  `ValidatorRunner._commands()` returns `[]` and the runner passes *vacuously*
  — it reports success and gates nothing. A validator worker with no commands
  configured is the same as no validator at all. Project page -> Validate
  commands.

**Not in Phase 0: MLX.** Ollama's MLX runner is on by default in current builds —
you get it by upgrading Ollama, not by editing dry-dock, and it's Mac-only so it
can't be a fleet-wide decision. ~20% throughput is real but small next to the
truncation bug, and shipping a runtime swap in the same window as an architecture
change means you won't know which one moved the number. Upgrade Ollama now for
the free win; run the runtime as a measured A/B *through* the Phase 1 harness.

### Phase 1 — The eval harness (~2 days)

- New `evals/`: pinned repo snapshot, ~20 task fixtures, a runner that drives a
  worker directly (no orchestrator, no DB).
- Score the five metrics above. One JSON row per (model, task, run).
- Run against today's code for a baseline.

### Phase 2 — The engineer loop (~1 week)

- New `worker/app/agent/`: `loop.py` (driver), `tools.py` (the seven tools),
  `budget.py` (caps + compaction).
- New `worker/app/runners/engineer.py` implementing `BaseRunner`'s interface so
  dispatch, artifacts, and logging are unchanged.
- Extend `GitWorkspace` with the read/grep/list primitives the tools wrap.
- Register `engineer` as a pool alongside `coder`. Race them on the harness;
  promote when it wins.
- Keep the SR text protocol as the non-tool-calling fallback.

### Phase 3 — Verification, and move the gate (~3 days)

- Tier-0 parsers wired into `write_file` (Vue, ts/js, py, json).
- `run_check` backed by named commands; add `Project.check_commands` as a
  name->command map so `validate_commands` keeps its current meaning.
- `renderer/` gains `POST /smoke`; engineer calls it as `run_check("smoke")`.
- **The gate move:** in `workers.py:_handle_result`, require a passing validation
  in the result payload before `apply_patch_and_push`. Unvalidated work still
  produces artifacts and a visible failure — it just doesn't reach a branch.

### Phase 4 — Fix the other roles (~3 days)

- **Reviewer:** clone the branch and compute the diff itself; delete the dead
  `payload["diff"]` path. `[blocker]` findings fail the task instead of returning
  `success=True`.
- **Planner:** give it `read_file` and `grep`. Validate at `materialize_plan` that
  every code task names <=3 real paths and reject otherwise — the "<=3 files" rule
  is currently only a suggestion in a prompt.
- **Tester:** actually run the suite it writes (`run_check` now exists).
- **Retry:** replace the prompt-append retry with a structured `previous_attempt`
  payload carrying the diff *and* the failure, so a re-queued task starts warm.

---

## 07 · What to expect

Phase 0 alone should produce a visible step change, because the model is currently
working almost blind. The "no SEARCH/REPLACE blocks in output" failure should
largely disappear the same day.

The honest limit: a ~30B-class local model in a good agent loop will still be
meaningfully behind a frontier model at greenfield app-building. No amount of
scaffolding closes that. What the loop *does* close is the reliability gap on
**small, well-specified changes with a fast verifier** — an achievable target that
matches how this system is meant to work. Many iterations are fine as long as
they're correct; that trade is why the verification ladder gets as much attention
here as the model choice.

It also means the planner's job gets more important, not less. The smaller and
more precisely specified each task, the more of them local models complete. If
after Phase 2 the harness still shows poor green rates, the next lever is *smaller
tasks*, not a bigger model.

Open question worth deciding early: whether to allow a cloud escape hatch for the
planner specifically. Planning is where a weak model does the most compounding
damage (a bad plan wastes every downstream task) and it's the cheapest step to buy
(once per goal). `InferenceProvider` already makes this a small change. Leave it
out of the first pass; revisit once the harness can price it.
