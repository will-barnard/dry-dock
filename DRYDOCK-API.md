# dry-dock Generate API

A single, stable HTTP endpoint for one-shot text generation on your local
worker fleet. Point any app at it, present the API key, get model output back
in the HTTP response. No WebSocket, no job polling, no browser session.

This is the request/response cousin of Operator chat: instead of streaming
deltas to a browser, `generate` blocks until the worker finishes and returns
the whole text at once — ideal for server-to-server calls (e.g. drafting sales
follow-ups, summaries, classifications) from another backend.

Drop this file into any project that needs to talk to dry-dock.

---

## Base URL

Whatever you set as `DRYDOCK_BASE_URL` for the orchestrator, e.g.:

```
https://drydock.your-domain.com
```

All endpoints below are relative to that.

## Authentication

Every request must present the **`DRYDOCK_API_KEY`** using either header:

```
X-API-Key: <your-key>
```

or

```
Authorization: Bearer <your-key>
```

This key is **separate** from `WORKER_SHARED_SECRET` (the worker fleet
credential) and from your operator login — rotate it independently.

Set it on the orchestrator (Beachhead dashboard, no Target Service so it lands
in `.env`):

```
DRYDOCK_API_KEY=<any long random string>   # e.g. openssl rand -hex 32
```

If `DRYDOCK_API_KEY` is unset/blank on the server, the API is **disabled** and
every call returns `503`.

---

## Endpoints

### `GET /api/v1/generate/health`

Cheap auth + liveness probe. Does not touch a worker.

```bash
curl -s https://drydock.your-domain.com/api/v1/generate/health \
  -H "X-API-Key: $DRYDOCK_API_KEY"
# → {"status":"ok"}
```

### `POST /api/v1/generate`

Run one inference and return the text.

**Request body** (JSON). Supply **either** `prompt` **or** `messages`:

| Field      | Type                         | Required | Notes |
|------------|------------------------------|----------|-------|
| `prompt`   | string                       | one of\* | Single user message. |
| `system`   | string                       | no       | System prompt, prepended when using `prompt`. |
| `messages` | array of `{role, content}`   | one of\* | Full chat history. Overrides `prompt` if both are sent. |
| `pool`     | string                       | no       | Pin a worker pool (`researcher`, `planner`, `docs`, `coder`, `reviewer`, `tester`, `refactorer`). Omit to auto-select (tries `researcher` first, then falls back). |
| `model`    | string                       | no       | Pin an Ollama model tag (e.g. `qwen2.5-coder:32b`). Omit to use the worker's default. |
| `timeout`  | number (seconds)             | no       | Per-request timeout. Capped by the server's `GENERATE_TIMEOUT_SECONDS` (default 120). |

\* At least one of `prompt` / `messages` must be non-empty.

**Response** `200`:

```json
{
  "content": "Hi Jane — great chatting on Tuesday about ...",
  "model": "qwen2.5-coder:32b",
  "worker": "macbook-researcher-1",
  "pool": "researcher",
  "elapsed_ms": 4213
}
```

**Error statuses**

| Status | Meaning |
|--------|---------|
| `401`  | Missing/invalid API key. |
| `422`  | Empty prompt/messages. |
| `503`  | API disabled (no server key) **or** no worker online in the requested pool. |
| `504`  | Worker didn't finish before the timeout. |
| `502`  | Worker reported an error, or the send failed. |

---

## Examples

### curl — simple prompt

```bash
curl -s https://drydock.your-domain.com/api/v1/generate \
  -H "X-API-Key: $DRYDOCK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "pool": "researcher",
    "system": "You are a concise sales assistant.",
    "prompt": "Draft a 3-sentence follow-up to Jane at Acme after a discovery call about their onboarding tooling."
  }'
```

### curl — full message history

```bash
curl -s https://drydock.your-domain.com/api/v1/generate \
  -H "Authorization: Bearer $DRYDOCK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-coder:32b",
    "messages": [
      {"role": "system", "content": "You write friendly, specific sales follow-ups."},
      {"role": "user", "content": "Lead: Jane Doe, Acme. Need: replace a brittle internal CRM. Next step: send a scoping doc. Write the email."}
    ]
  }'
```

### Node / Express client

```js
// drydock.js — drop-in client
export async function generate({ prompt, system, messages, pool, model, timeout }) {
  const res = await fetch(`${process.env.DRYDOCK_BASE_URL}/api/v1/generate`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": process.env.DRYDOCK_API_KEY,
    },
    body: JSON.stringify({ prompt, system, messages, pool, model, timeout }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`dry-dock generate ${res.status}: ${text}`);
  }
  return res.json(); // { content, model, worker, pool, elapsed_ms }
}
```

### Python client

```python
import os, httpx

async def generate(prompt=None, *, system=None, messages=None,
                   pool=None, model=None, timeout=None):
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(
            f"{os.environ['DRYDOCK_BASE_URL']}/api/v1/generate",
            headers={"X-API-Key": os.environ["DRYDOCK_API_KEY"]},
            json={"prompt": prompt, "system": system, "messages": messages,
                  "pool": pool, "model": model, "timeout": timeout},
        )
        r.raise_for_status()
        return r.json()
```

---

## Server-side env vars

Set these on the orchestrator (Beachhead → Environment, no Target Service):

| Var                        | Default | Purpose |
|----------------------------|---------|---------|
| `DRYDOCK_API_KEY`          | *(empty)* | Key callers must present. Empty = API disabled. |
| `GENERATE_TIMEOUT_SECONDS` | `120`   | Hard ceiling per generate call. |

## How it works (for maintainers)

`generate` reuses the existing Workbench message pair
(`workbench_request` / `workbench_result`) with a dedicated `kind="generate"`.
The worker's `run_workbench_job` is kind-agnostic — it just runs
`provider.chat(model, messages)` and returns the content — so **the worker
needs no changes** to support this API.

The only new machinery is on the orchestrator:

- `app/orchestrator/generate.py` — picks a live worker, sends the request, and
  awaits an `asyncio.Future` keyed by `job_id`.
- `app/routes/generate.py` — the FastAPI router + API-key auth.
- `app/routes/workers.py` — when a `workbench_result` arrives, `resolve_generate`
  gets first crack; if the `job_id` belongs to a generate call it resolves the
  Future and the DB Workbench handlers are skipped. Worker disconnect rejects
  any waiting Futures.

This assumes a single orchestrator replica (same assumption as Operator chat):
the worker's WebSocket and the awaiting HTTP request live in the same process.
Multi-replica support would need a shared result bus.
