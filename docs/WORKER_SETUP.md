# Deploying a worker

Workers run on whatever Mac you want — usually the Mac mini and the MacBook —
and connect outbound to the orchestrator on Beachhead. They never need an
inbound port open.

## Prerequisites

- macOS with Docker Desktop installed
- Ollama installed on the host (not inside Docker — keeping it on the host
  lets Ollama use the Metal-backed Apple Silicon inference path)
- Network access to your dry-dock URL (`wss://drydock.your-domain.com`)

```bash
brew install --cask docker
brew install ollama

# In one terminal, leave this running (or use `brew services start ollama`)
ollama serve
```

## Pull the models you want this worker to serve

Worker registration tells the orchestrator which Ollama models are locally
available, and the dispatcher routes tasks based on that. Pull every model
this machine should be able to handle.

```bash
# Default coding model for this project
ollama pull qwen2.5-coder:32b

# Optional alternatives
ollama pull deepseek-coder-v2:16b
ollama pull devstral:24b
```

## Configure the worker

```bash
git clone https://github.com/will-barnard/dry-dock.git
cd dry-dock/worker
cp .env.example .env
```

Edit `.env`:

| Var | Example | Notes |
|---|---|---|
| `ORCHESTRATOR_URL` | `wss://drydock.your-domain.com/ws/worker` | Public URL of the Beachhead deploy with the `/ws/worker` path. |
| `WORKER_SHARED_SECRET` | (paste) | Must match the value set in Beachhead env. |
| `WORKER_NAME` | `macbook-coder-1` | Globally unique. Used as the registry key. |
| `WORKER_POOL` | `coder` | One of `planner | coder | reviewer | tester | refactorer | docs | researcher`. |
| `HARDWARE_CLASS` | `macbook` | Free-form. Used for routing hints. |
| `RAM_GB` | `64` | Honest advertised RAM. |
| `MAX_CONTEXT` | `32768` | Max context tokens this worker can handle. |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Default works on Docker Desktop. |
| `DEFAULT_MODEL` | `qwen2.5-coder:32b` | Fallback when a task doesn't specify a preferred model. |

## Start it

```bash
docker compose -f docker-compose.example.yml up -d
docker compose logs -f
```

You should see `worker.registered` shortly. Open the dry-dock dashboard — the
worker appears in the live-workers panel with its pool and status.

## Running multiple pools on one machine

The MacBook is a good candidate for serving both `coder` and `planner` (both
benefit from the big model). Copy the directory and run two compose projects
with different `WORKER_NAME` / `WORKER_POOL`:

```bash
cp -r dry-dock dry-dock-planner
cd dry-dock-planner/worker
# edit .env: WORKER_NAME=macbook-planner-1, WORKER_POOL=planner
COMPOSE_PROJECT_NAME=drydock-planner docker compose -f docker-compose.example.yml up -d
```

Each instance gets a separate Docker network and worktree directory.
Single-task-at-a-time per worker is intentional; parallelism comes from
running more containers.

## Tuning

- **Ollama threads / GPU layers.** Edit `~/.ollama/config.json` if you need
  to bound concurrent inference. Apple Silicon usually does the right thing.
- **Worktree disk usage.** Each job clones the repo shallowly into
  `./worktrees/` and cleans up after itself. If you see leftovers from
  crashed workers, it's safe to `rm -rf worker/worktrees/*` between runs.
- **Watching memory.** `docker stats` and `top -o MEM` on the host are your
  friends. If a model OOMs Ollama, the worker will surface the error as a
  failed task; consider declaring a higher `min_ram_gb` on the task or a
  smaller model.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Worker logs `auth failed` / closes with code 4401 | `WORKER_SHARED_SECRET` doesn't match the orchestrator's. |
| `ConnectionRefusedError` to Ollama | `ollama serve` not running on the host, or `OLLAMA_BASE_URL` is wrong. Test: `curl http://localhost:11434/api/tags` from the host. |
| Worker reconnects every few seconds | Check Beachhead nginx WS timeouts (our `nginx.conf` already sets these long) and that the URL path is `/ws/worker`. |
| Task stuck `claimed` | The dispatcher granted but the worker disconnected before `job_started`. The next dispatcher tick (≤5s) requeues. If it doesn't, restart the worker. |
