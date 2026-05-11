# Architecture

## Services

| Service | Image / build | Role | Beachhead role |
|---|---|---|---|
| `frontend` | `./frontend` (nginx) | Public HTTPS terminator + WS upgrade proxy. Renders nothing — proxies everything to backend. | `public_service` |
| `backend` | `./backend` (FastAPI / uvicorn) | Orchestrator: REST API, HTMX dashboard, worker WS hub, dispatcher, planner-fan-out, git service, GitHub PR opener. | normal service |
| `postgres` | `pgvector/pgvector:pg16` | Durable state: projects, tasks, runs, events, artifacts, workers, approval gates. pgvector ready for memory work. | **stateful** (declared in `beachhead.json`) |

Volumes:

- `dry-dock-postgres` (fixed name) — Postgres data dir.
- `dry-dock-repos` — orchestrator's cached clones of each project repo.

Network: a single `internal` bridge network. Every service declares it
explicitly; nothing uses `ports:` (Beachhead's nginx-proxy routes public
traffic in by service name).

## Data model

```
Project ── Task ── Run ── Event
            │       │
            │       └── Artifact
            │
            └── ApprovalGate

Worker (independent of tasks; runtime view lives in-process)
```

- `Project` — repo coordinates + approval policy + system prompt.
- `Task` — one unit of agent work. Has `kind` (plan/code/review/test/refactor/docs/research),
  `required_pool`, capability requirements, attempt counter, optional `parent_task_id`
  forming a DAG.
- `Run` — one execution attempt of a task on a worker.
- `Event` — log/error/status entries belonging to a run (streamed live via SSE).
- `Artifact` — outputs of a run: `patch` diffs, `text` reports, `summary` (planner JSON), etc.
- `ApprovalGate` — `plan` or `merge` decision pending a human. Resolves the task.
- `Worker` — durable record of a worker. The live WS connection lives in `WorkerRegistry`.

## Worker ↔ orchestrator protocol

Wire: JSON-over-WebSocket. Workers connect outbound to
`wss://<host>/ws/worker?token=<shared-secret>`. First message is `register`;
last meaningful message before disconnect is usually a `result`.

**Worker → orchestrator**: `register`, `heartbeat`, `claim_request`,
`job_started`, `log`, `artifact`, `result`, `error`.

**Orchestrator → worker**: `welcome`, `claim_grant`, `cancel`, `ping`.

The full pydantic-validated definitions live in
`backend/app/orchestrator/protocol.py` and the mirror in
`worker/app/protocol.py`. Keep them in sync — there's a verification step in
the build that will eventually enforce this.

## Sequence: code task end-to-end

```
user                          orchestrator                       worker (coder pool)
 │  POST /projects/.../tasks       │                                   │
 ├────────────────────────────────►│                                   │
 │                                 │  insert Task(QUEUED)              │
 │                                 │  dispatcher tick                  │
 │                                 │  select_worker_for_task           │
 │                                 │  claim_grant ───────────────────► │
 │                                 │                                   │  clone repo
 │                                 │ ◄──── job_started                 │  call ollama (stream)
 │                                 │ ◄──── log * N (SSE → dashboard)   │  parse diff
 │                                 │ ◄──── artifact(patch)             │
 │                                 │ ◄──── result(success=true)        │
 │                                 │  apply patch on agent/<task-id>   │
 │                                 │  push to GitHub                   │
 │                                 │  open PR                          │
 │                                 │  insert ApprovalGate(MERGE)       │
 │  GET /tasks/<id>                │                                   │
 │ ◄─────────────────────────────  │  HTML w/ approve button           │
 │  POST /approvals/<id> approve   │                                   │
 ├────────────────────────────────►│  mark gate APPROVED, task SUCCEEDED
```

## Dispatch & routing

- `dispatcher.py` runs a single async task in-process. On each tick (or poke),
  it iterates over `KNOWN_POOLS`, finds the highest-priority `QUEUED` task per
  pool, then calls `router.select_worker_for_task` to pick a live worker that
  satisfies the task's RAM / context / model requirements.
- `Task` rows are claimed under `SELECT … FOR UPDATE SKIP LOCKED` — safe to
  run multiple orchestrator replicas eventually.

## Approval gates

Default project policy is **gated** for both plan and merge. A project can
flip either toggle on for auto-approval. Gates live in the `approval_gates`
table and are resolved by `POST /approvals/<gate_id>` from the dashboard.

For a `PLAN` gate, approving triggers `materialize_plan()` which expands the
plan artifact (JSON array of task specs) into real `Task` rows wired up by
`depends_on` index.

For a `MERGE` gate, approving marks the task succeeded. The actual GitHub PR
was opened when the patch was first applied; merging the PR remains the
human's call (we never auto-merge to `main`).

## Streaming

- Workers send `log` messages over WS as the model streams.
- The backend persists each chunk to `events` and republishes it on the
  in-process `EventBus`.
- SSE endpoint `/stream/tasks/<task-id>` subscribes to that topic and pushes
  events to the dashboard, where HTMX's `sse` extension swaps them into the
  log viewer in real time.

## Nginx & Beachhead specifics

`frontend/nginx.conf` uses Docker's embedded DNS resolver with a variable for
`proxy_pass`, so the backend hostname is resolved at request time rather than
config-load time — this is the fix for the cold-start `host not found in
upstream` crash. `ipv6=off` is required.

`/ws/` and `/stream/` locations have `proxy_buffering off` and long read
timeouts; everything else uses default proxying.

`beachhead.json` declares `postgres` as a stateful service so it isn't
recreated on each blue/green swap.
