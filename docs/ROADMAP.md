# Roadmap

The plan, in order. Items in **bold** are believed already shipped in the MVP
scaffold; everything else is work to do.

## Phase 0 — MVP scaffold (shipped)

- **Beachhead-compatible compose stack** (frontend/backend/postgres, internal
  network, fixed volume names, embedded DNS resolver in nginx, stateful
  postgres declaration).
- **FastAPI orchestrator** with projects/tasks/workers REST API + HTMX dashboard.
- **Worker WebSocket protocol** with register/claim/log/artifact/result and
  pydantic-validated message types on both sides.
- **Worker runtime** with one runner per pool: planner, coder, reviewer,
  tester, refactorer, docs, researcher.
- **Capability-aware routing** — workers advertise pool, RAM, max_context,
  installed models; orchestrator picks the best match.
- **GitHub integration** — clone-on-create, patch-apply on `agent/<task-id>`
  branch, force-with-lease push, draft PR opener.
- **Approval gates** for plan fan-out and merge.
- **Live log streaming** via SSE → HTMX.

## Phase 1 — make the loop dependable

Goal: a plan→code→review→merge cycle that runs cleanly without babysitting.

- [ ] **Retry hardening.** Today a failed task re-queues up to `max_attempts`
  times with no backoff and no context about why it failed. Add (a) exponential
  backoff via a `scheduled_at` column, (b) including the previous failure's
  stderr in the retry's user prompt.
- [ ] **Patch validation before push.** Run `git apply --check` in the
  orchestrator before committing, and if it fails, send the error back to the
  worker as a structured `cancel` with a retry hint so the model can fix its
  diff. Today a malformed diff fails the task hard.
- [ ] **Test runner that actually runs tests.** The `tester` runner only
  emits a diff today. Add a sandboxed `pytest` / `npm test` / `cargo test`
  execution step inside the worker container, with results streamed as a
  `test_report` artifact and a structured pass/fail in the result payload.
- [ ] **Reviewer pulls the diff from the orchestrator** instead of relying on
  it being in `payload.diff`. New endpoint `GET /api/tasks/<id>/artifacts/latest?kind=patch`.
- [ ] **Auto-chained tasks.** When a coder task succeeds, the orchestrator
  should automatically queue a reviewer task on the same branch (still subject
  to the merge gate). Config per project: `auto_chain_review: bool`.
- [ ] **Worker cancellation that actually aborts the runner.** Right now we
  log the cancel and continue. Wire it to a `cancellation_event` the runner's
  Ollama call watches.

## Phase 2 — sharper specializations

Each pool gets the depth treatment.

- [ ] **Planner**
  - Tool-calling support (Ollama function-calling) so the planner can ask for
    a partial file read before committing to a plan.
  - Cost estimate per step (token budget, expected runtime).
  - Re-planning: when a child task fails twice, automatically queue a new
    `plan` task with the failure context.
- [ ] **Coder**
  - Embedding-backed retrieval over the repo (Postgres + pgvector). Replace
    the dumb file-tree dump with the top-k most relevant files.
  - Multi-turn within a single task (ask for a clarifying read, then produce
    the diff).
  - Symbol-aware prompts via tree-sitter snippets for the files being touched.
- [ ] **Reviewer**
  - Static analysis pass (ruff / mypy / eslint / tsc, language-detected) and
    inject findings into the review prompt.
  - Severity-aware verdict: any `[blocker]` finding automatically rejects.
- [ ] **Tester**
  - Coverage-aware: detect uncovered branches in the diff and weight tests
    toward them.
  - Property-based testing scaffolding for languages that support it.
- [ ] **Refactorer**
  - Behavior-equivalence check: before/after run of the existing test suite.
- [ ] **Docs**
  - Auto-update README/CHANGELOG on every merged feature task.
- [ ] **Researcher**
  - Web fetch with a domain allowlist (or none — pure local LLM only,
    configurable).

## Phase 3 — autonomy

The goal here is to relax the approval gates safely.

- [ ] **Per-project auto-approval rules.** Already a boolean today; expand to
  a small policy DSL: "auto-approve plans with ≤5 steps", "auto-approve merges
  to branches under `experimental/*`".
- [ ] **Confidence scoring.** Each runner returns a self-reported confidence;
  low confidence forces a gate even on auto-approve projects.
- [ ] **Multi-replica orchestrator.** Move the dispatcher poke channel from
  in-process to Postgres `LISTEN/NOTIFY`. Move the event bus to the same.
- [ ] **Persistent autonomous agents.** A long-running planner that wakes up
  on a schedule, reviews open PRs and outstanding TODOs in a repo, and queues
  follow-up tasks.

## Phase 4 — heterogeneous routing

- [ ] **MLX backend** as a sibling of OllamaProvider for models that need the
  speed.
- [ ] **vLLM backend** for batch workloads on a Linux GPU box (if/when one
  shows up).
- [ ] **Cloud fallback.** If no local worker can serve a task within N
  seconds, route to an Anthropic/OpenAI/Bedrock backend with an explicit
  per-project ceiling (cost cap).
- [ ] **Model preference profiles.** A project can declare "prefer
  qwen2.5-coder:32b for code, deepseek-r1:7b for review, devstral for plan."

## Phase 5 — operability

- [ ] Metrics endpoint (Prometheus) — task latency, queue depth per pool,
  tokens/s per worker, retry rate.
- [ ] Per-task token-cost ledger and per-project budgets.
- [ ] Audit log: every approval, every push, every PR opened, with the
  prompt + diff hashes.
