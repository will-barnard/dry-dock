# dry-dock

A local-first distributed AI software-engineering platform for Apple Silicon Macs.

A FastAPI orchestrator on Beachhead coordinates specialized worker pools — planner, coder, reviewer, tester, refactorer, docs, researcher — that run on a Mac mini and MacBook (or any host with Ollama). Workers connect outbound over a single WebSocket, advertise their hardware and installed models, claim jobs, run them in isolated git workspaces, and stream logs and unified-diff patches back. The orchestrator owns the GitHub clone, applies approved patches, and opens PRs.

## How it fits together

```
┌──────────────────────── Beachhead VPS ────────────────────────┐
│  frontend (nginx)  ◀── public HTTPS / WSS                     │
│        │                                                       │
│  backend (FastAPI)  ── orchestrator, HTMX UI, REST API,       │
│        │                workers WS hub, planner state machine  │
│  postgres (pgvector) ── stateful, fixed-name volume           │
└────────────────────────────────────────────────────────────────┘
                          ▲    ▲
            wss://.../ws/worker  (outbound, long-lived)
                          │    │
   ┌──────────────────────┘    └──────────────────────┐
   │                                                  │
┌──┴─── Mac mini ────┐                  ┌─────── MacBook (high RAM) ──────┐
│  worker(s)         │                  │  worker(s)                       │
│   pool: planner    │                  │   pools: coder, reviewer, tester │
│  Ollama (host)     │                  │  Ollama (host)                   │
└────────────────────┘                  └──────────────────────────────────┘
```

## Quickstart (orchestrator)

1. Push to your GitHub repo and let Beachhead build the deploy.
2. In the Beachhead dashboard set these env vars (no Target Service so they land in `.env`):
   - `DB_PASSWORD` — any strong password
   - `WORKER_SHARED_SECRET` — any strong random string (workers present it on connect)
   - `SESSION_SECRET` — any long random string used to sign the login cookie. If you rotate it, every existing browser session is invalidated. Generate with `openssl rand -hex 32`.
   - `GITHUB_TOKEN` — PAT with `repo` scope
   - `GITHUB_USERNAME` — your GitHub username
   - `DRYDOCK_BASE_URL` — e.g. `https://drydock.your-domain.com`
3. Trigger a deploy.
4. Open the public URL. The first visit redirects to `/setup`, which prompts you to create the admin account. Once that's done, `/setup` becomes inaccessible and every subsequent visit goes to `/login`.

## Quickstart (worker on a Mac)

```bash
# On the Mac
brew install ollama
ollama serve &        # http://localhost:11434
ollama pull qwen2.5-coder:32b

git clone https://github.com/will-barnard/dry-dock.git
cd dry-dock/worker
cp .env.example .env
# edit .env: set ORCHESTRATOR_URL, WORKER_SHARED_SECRET, WORKER_NAME, WORKER_POOL, RAM_GB, etc.

docker compose -f docker-compose.example.yml up -d
docker compose logs -f
```

Bring up one worker per pool you want this Mac to serve. The MacBook is the
natural home for `coder` and `planner` (where the bigger context windows
matter); the Mac mini is fine for `docs`, `researcher`, `reviewer`, `tester`,
`refactorer`.

## Using it

1. Create a project pointed at a GitHub repo you own (create an empty repo first).
2. Dispatch a `plan` task with a short goal in the prompt.
3. Approve the plan when it lands. dry-dock will fan out the child tasks to the right pools.
4. Approve the merge gate on each completed task to push the PR.

See `docs/ARCHITECTURE.md` for the message protocol and data model, and
`docs/ROADMAP.md` for what's MVP and what's next.
