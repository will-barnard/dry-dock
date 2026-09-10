# dry-dock Image API

`POST /api/v1/image` renders an image on your own GPU — the Darkroom module's
pipeline, exposed to any app holding `DRYDOCK_API_KEY`. Same key and same base
URL as the Generate API; drop this file into the consuming repo the way you
would `DRYDOCK-API.md`.

**The one thing to know before you write a client:** unlike
`/api/v1/generate`, this endpoint does **not** block until the image is ready.
It can't. The GPU box sleeps, and a cold request is wake + boot + Docker +
checkpoint load + render — 60 to 170 seconds — against HTTP clients that give
up long before that. So the contract is submit → poll, with an optional short
`wait` for the case where the machine is already awake.

---

## Base URL

Same as generate: whatever `DRYDOCK_BASE_URL` is set to
(e.g. `https://drydock.bonetothebad.com`).

## Authentication

`X-API-Key: <key>` or `Authorization: Bearer <key>`, carrying
`DRYDOCK_API_KEY`. This is a different credential from
`WORKER_SHARED_SECRET` (the fleet) and from the operator login — all three
rotate independently. A blank `DRYDOCK_API_KEY` on the server disables both
public APIs entirely (503), deliberately, so a deploy that never set the key
can't be probed with a blank-vs-blank comparison.

---

## Endpoints

### `GET /api/v1/image/health`

Auth + readiness probe. Touches no worker.

```bash
curl -s "$DRYDOCK_BASE_URL/api/v1/image/health" -H "X-API-Key: $DRYDOCK_API_KEY"
# → {"status":"ok","imagers":1,"workers":[{"name":"windows-imager-1","busy":false,
#     "checkpoints":["juggernautXL_v9.safetensors"],"workflows":["sdxl_txt2img"]}],
#    "machine":"windows-rig","machine_online":null,"can_wake":true}
```

`status` is `ok` only when an imager is actually connected, and `no_imager`
otherwise — so unlike `/generate/health` this **is** a usable readiness gate.
`machine_online` is `null` when it wasn't checked (an imager was already there).

### `POST /api/v1/image`

```jsonc
{
  "prompt": "a weathered Rhodes piano on a foggy beach at dawn, soft light",
  "negative_prompt": "blurry, watermark, text",   // optional
  "workflow": "sdxl_txt2img",   // optional; named template on the worker
  "checkpoint": null,           // optional; omit for the worker's default
  "width": 1024,                // clamped 256-2048, rounded to a multiple of 8
  "height": 1024,
  "steps": 30,                  // clamped 1-80
  "cfg": 6.0,
  "sampler": null,              // optional; omit to use the template's
  "scheduler": null,
  "seed": null,                 // omit for random — the seed used comes back
  "batch": 1,                   // capped by IMAGE_MAX_BATCH (default 4)
  "init_image_b64": null,       // img2img — see below
  "denoise": null,              // only with init_image_b64; defaults to 0.6
  "wait": null                  // optional; see below
}
```

### Starting from an image (img2img)

Pass `init_image_b64` and the job transforms your picture instead of starting
from noise. Any common format; it's decoded, flattened to RGB and resized to
about a megapixel server-side, because SDXL is trained near that and a
full-size phone photo is both slower and worse.

Three consequences worth knowing:

- **The output takes its dimensions from your image**, not from `width`/
  `height`. Those are ignored.
- **The workflow switches** to `IMAGE_IMG2IMG_WORKFLOW` (default
  `sdxl_img2img`) whatever you asked for, because a text-to-image graph has
  nowhere to put a source image and silently ignoring the upload would be
  worse.
- **`denoise` is the control that matters.** ~0.3 retouches, ~0.6 restyles,
  ~0.85 keeps little more than the composition, 1.0 discards the image
  entirely. It's ignored without `init_image_b64`.

```bash
curl -s "$DRYDOCK_BASE_URL/api/v1/image" \
  -H "X-API-Key: $DRYDOCK_API_KEY" -H "Content-Type: application/json" \
  -d "{\"prompt\":\"clean product photo on seamless white\",
       \"denoise\":0.4,
       \"init_image_b64\":\"$(base64 -w0 rhodes.jpg)\"}"
```

A file that isn't a decodable image comes back 422 before any job is created,
so a bad upload fails at the call rather than deep inside a render.

Returns **202** with the job:

```json
{
  "job_id": "3f1c…",
  "status": "pending",
  "poll_url": "https://drydock…/api/v1/image/3f1c…",
  "prompt": "a weathered Rhodes piano…",
  "images": [],
  "checkpoint": null,
  "worker": null,
  "error": null
}
```

`wait` (seconds, capped at 60) blocks for a finished result and returns **200**
if it lands in time. It degrades to 202 — never a 504 — so it's safe to pass
optimistically. Use it when the machine is likely awake; ignore it otherwise.

### `GET /api/v1/image/{job_id}`

The same shape, with `status` moving through
`pending → waking → running → done | error`. When `done`:

```json
{
  "status": "done",
  "images": [{
    "index": 0,
    "url": "https://drydock…/api/v1/image/3f1c…/file/0.png",
    "thumb_url": "https://drydock…/api/v1/image/3f1c…/file/0_thumb.webp",
    "seed": 918273645, "width": 1024, "height": 1024
  }],
  "checkpoint": "juggernautXL_v9.safetensors",
  "worker": "windows-imager-1"
}
```

`checkpoint` here is **what actually ran**, not what you asked for. (The
generate API's `model` field echoes the request; this one doesn't. Log this
one.) If the checkpoint you named isn't installed, the worker falls back to one
that is rather than failing, and says so here.

### `GET /api/v1/image/{job_id}/file/{index}.png`

The bytes. Also `{index}_thumb.webp` for a ≤512px thumbnail. Both need the API
key — the images volume is never publicly served.

---

## Status codes

| Code | Means | Client should |
|---|---|---|
| 202 | accepted, rendering | poll `poll_url` |
| 200 | finished within `wait` | use `images` |
| 401 | bad/missing key | fail loudly — config error, never retry |
| 422 | empty prompt | fail loudly — caller bug |
| 429 | `IMAGE_DAILY_BUDGET` reached | back off until tomorrow |
| 503 | API disabled, **or** no imager and no machine to wake | degrade; don't hammer |
| 404 | unknown job or image index | — |

A render that fails *after* acceptance is **not** an HTTP error: the job comes
back `status: "error"` with a human-readable `error`. Branch on `status`, not
just the code.

Poll about every 2 seconds, and budget ~180s for a cold job before giving up —
that's `IMAGE_WAKE_TIMEOUT_SECONDS` plus a render.

---

## Node client

```js
// drydock-image.js — needs DRYDOCK_BASE_URL and DRYDOCK_API_KEY in env.
const BASE = process.env.DRYDOCK_BASE_URL;
const KEY = process.env.DRYDOCK_API_KEY;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function generateImage(prompt, opts = {}) {
  const { pollMs = 2000, timeoutMs = 180000, ...params } = opts;

  const res = await fetch(`${BASE}/api/v1/image`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-API-Key": KEY },
    body: JSON.stringify({ prompt, ...params }),
  });
  if (res.status === 401) throw new Error("drydock: bad API key");
  if (res.status === 429) throw new Error("drydock: daily image budget reached");
  if (!res.ok && res.status !== 202) {
    // 503 here means no imager and nothing to wake — degrade, don't retry.
    throw new Error(`drydock: ${res.status} ${await res.text()}`);
  }

  let job = await res.json();
  if (job.status === "done") return job;

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await sleep(pollMs);
    const r = await fetch(job.poll_url, { headers: { "X-API-Key": KEY } });
    if (!r.ok) throw new Error(`drydock: poll failed ${r.status}`);
    job = await r.json();
    if (job.status === "done") return job;
    if (job.status === "error") throw new Error(`drydock: ${job.error}`);
  }
  // Don't treat this as fatal — the job is still running server-side and will
  // land in Darkroom. Hand back the id so the caller can pick it up later.
  throw Object.assign(new Error("drydock: image timed out client-side"),
                      { jobId: job.job_id });
}

export async function fetchImageBytes(url) {
  const r = await fetch(url, { headers: { "X-API-Key": KEY } });
  if (!r.ok) throw new Error(`drydock: image fetch ${r.status}`);
  return Buffer.from(await r.arrayBuffer());
}
```

## Python client

```python
# drydock_image.py — needs DRYDOCK_BASE_URL and DRYDOCK_API_KEY in env.
import os, time
import httpx

BASE = os.environ["DRYDOCK_BASE_URL"].rstrip("/")
KEY = os.environ["DRYDOCK_API_KEY"]
_H = {"X-API-Key": KEY}


class DrydockImageError(RuntimeError):
    pass


def generate_image(prompt: str, *, poll_s: float = 2.0,
                   timeout_s: float = 180.0, **params) -> dict:
    with httpx.Client(timeout=30.0) as c:
        r = c.post(f"{BASE}/api/v1/image", headers=_H, json={"prompt": prompt, **params})
        if r.status_code == 401:
            raise DrydockImageError("bad API key")
        if r.status_code == 429:
            raise DrydockImageError("daily image budget reached")
        if r.status_code not in (200, 202):
            raise DrydockImageError(f"{r.status_code}: {r.text}")

        job = r.json()
        deadline = time.monotonic() + timeout_s
        while job["status"] not in ("done", "error"):
            if time.monotonic() > deadline:
                raise DrydockImageError(f"timed out client-side; job {job['job_id']} may still finish")
            time.sleep(poll_s)
            job = c.get(job["poll_url"], headers=_H).json()

        if job["status"] == "error":
            raise DrydockImageError(job["error"])
        return job


def fetch_image_bytes(url: str) -> bytes:
    with httpx.Client(timeout=60.0) as c:
        r = c.get(url, headers=_H)
        r.raise_for_status()
        return r.content
```

---

## Server-side env vars

Set in the Beachhead dashboard with no Target Service so they land in `.env`:

| Var | Default | Notes |
|---|---|---|
| `DRYDOCK_API_KEY` | — | shared with the generate API; blank disables both |
| `IMAGE_MACHINE` | — | **the important one.** A `name` from `REMOTE_MACHINES_JSON`. Blank means no auto-wake, and a request with no imager online 503s instead |
| `IMAGE_WAKE_TIMEOUT_SECONDS` | 180 | wake → boot → Docker → worker registers |
| `IMAGE_JOB_TIMEOUT_SECONDS` | 300 | ceiling on one render once dispatched |
| `IMAGE_MAX_BATCH` | 4 | per-request image cap |
| `IMAGE_IMG2IMG_WORKFLOW` | `sdxl_img2img` | workflow used when a request carries a source image |
| `IMAGE_MAX_UPLOAD_MB` | 25 | ceiling on a source image before resizing |
| `IMAGE_DAILY_BUDGET` | 0 | API-sourced images per day; 0 = no cap |
| `IMAGE_RETENTION_DAYS` | 0 | delete rendered files after N days; 0 = keep |
| `IMAGE_DEFAULT_WORKFLOW` | `sdxl_txt2img` | used when a request names none |

---

## How it works (for maintainers)

`POST` writes an `image_jobs` row and starts a background driver task. The
driver finds a live imager — waking `IMAGE_MACHINE` through the host agent if
there isn't one, which is why this path doesn't use the cookie-gated
`/api/machines/{name}/wake` — then sends an `image_request` over the worker
WebSocket. The imager renders on ComfyUI and returns **one `image_result`
message per image**; the driver writes each PNG plus a thumbnail to the
`dry-dock-images` volume and flips the row to `done`.

Image work is the one thing that could not reuse the Workbench message pair:
`run_workbench_job` is hard-wired to a single `provider.chat` returning a
string, so an un-upgraded worker would answer an image job with prose. Hence a
dedicated pair, and a `capabilities` list on `RegisterMsg` that old workers
simply omit — which is what lets the fleet upgrade one machine at a time.

Same single-replica assumption as chat and generate: the driver task and the
worker's WebSocket live in the same process. Multi-replica needs the shared
result bus that's already on the roadmap.

See `docs/specs/darkroom-image-generation.md` for the full design, and
`docs/DARKROOM-SETUP.md` for standing the GPU box up.
