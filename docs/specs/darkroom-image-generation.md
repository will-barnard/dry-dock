# Darkroom — an image generation module

**Status:** built (Sept 2026) — phases 1-3 below are implemented; see
`docs/DARKROOM-SETUP.md` for what still has to happen on the Windows box
**Author:** Will + assistant pair
**Working name:** Darkroom (alternatives: Studio, Foundry, Lantern)
**Depends on:** nothing shipped. Needs ComfyUI standing up on the Windows box first.

---

## The problem this solves

dry-dock runs inference on hardware Will owns, and it cannot make an image. That
gap is structural rather than cosmetic: **every** inference path in the codebase
terminates at `provider.chat(model, messages)` against Ollama, and Ollama has no
diffusion backend. Engineer runners, Operator chat, Workbench jobs and
`/api/v1/generate` all funnel through that one call — `generate` exists at all
because `run_workbench_job` is kind-agnostic and returns a string.

So image generation is not a new prompt. It is **a second inference substrate**
alongside Ollama, and the design question is how to add one without disturbing
the text fleet that already works.

Decisions taken up front (Will, Sept 2026):

- **General creative text-to-image** is the job. Listing-photo work for
  Gearline (img2img, background replacement, inpainting) is a natural
  follow-on, not phase 1.
- Inference runs **locally on the Windows RTX 5080**, not in the cloud.
- First cut ships **both** a module UI and a keyed `/api/v1/image` endpoint.
- An Operator `generate_image` tool is wanted on top, once the module exists.

---

## Why ComfyUI on the Windows box — and what it costs

16 GB of VRAM is a comfortable SDXL machine, a comfortable Flux-schnell machine,
and a tight-but-workable Flux-dev-fp8 machine. ComfyUI is the right host for it:
it is HTTP-addressable (`POST /prompt` → `prompt_id`, poll `/history/{id}`), it
keeps checkpoints resident between calls, and its workflow-graph format means a
new style is a JSON change rather than code.

Three costs come with that choice, and all three are properties of *that
machine* rather than of ComfyUI.

**The box sleeps.** It answers Wake-on-LAN via the host-agent and shuts itself
down when idle. Every latency number in this spec is therefore bimodal: ~5–20 s
warm, ~60–150 s from cold (wake + boot + Docker + checkpoint load). This is the
single fact that shapes the API design below.

**Waking it is behind the wrong auth.** `POST /api/machines/{name}/wake` sits
inside the session-gated router, so an external app holding `DRYDOCK_API_KEY`
cannot wake the machine it needs. The module UI can (it has the cookie); the API
cannot. Fixed below with an internal helper rather than by loosening that route.

**The GPU is already spoken for.** Windows also runs `reviewer`, `tester` and
`validator` against its host Ollama. A resident SDXL checkpoint (~7 GB) plus a
resident `qwen2.5-coder:14b` (~9 GB) does not fit in 16 GB, so a review task
arriving mid-render will evict one or the other and both get slow. Two honest
options: accept the serialization, or — while Darkroom is live — raise the
`worker_priority` integers on `windows-reviewer` / `windows-tester` so text work
prefers the MacBook tier. Note that priority is **DB state in `app_settings`,
set on the Settings page**, not the env-file comments; and that the router waits
on a busy primary rather than dropping to a backup, so the tier ordering is what
actually decides this, not idleness.

---

## Architecture — where it plugs in

### 1. A new `imager` worker on the existing worker runtime

Extend the current worker container with a ComfyUI provider and let it run with
`WORKER_POOL=imager`, rather than building a separate image service. It needs no
git and no Ollama, and it adds **no new dependency at all** — `httpx` is already
in `worker/requirements.txt`, and ComfyUI lives at
`http://host.docker.internal:8188` (Docker Desktop wires that on Windows exactly
as it does on the Mac). In exchange it inherits registration, heartbeat,
disconnect handling, the reconnect loop, `workers.sh`, and the `envs/*.env`
muscle memory — none of which is worth re-implementing.

Registration stays backward compatible by being **additive**:

```python
class RegisterMsg(BaseModel):
    ...
    capabilities: list[str] = []      # e.g. ["chat"] or ["image"]
    # metadata carries {"workflows": [...], "checkpoints": [...]} for imagers
```

Pydantic ignores unknown fields in both directions, so an un-upgraded MacBook
worker registers exactly as it does today and machines upgrade one at a time.

**Two traps to route around deliberately:**

- **Keep `imager` out of `KNOWN_ROLES`.** A role→model pin is a *hard filter*
  (`get_role_model_if_set` → `_worker_compatible`) matching the exact string
  against `installed_models`. An imager advertises checkpoint filenames, not
  Ollama tags; the moment someone saves the Settings form, the pin would drop
  every imager and the pool would silently route to nobody. That is finding F1
  in `ENGINEER-REBUILD.md` §05 repeating itself with new nouns.
- **Keep `imager` out of `KIND_TO_POOL`.** Image jobs have no Task/DAG/git
  lifecycle — they use the direct registry pick that `generate` and Workbench
  use, not the dispatcher. Adding it to `KNOWN_POOLS` buys only one no-op query
  per dispatcher tick; add it only if the workers page wants it for grouping,
  and if you do, remember `validator` is the cautionary tale (F2: in
  `KNOWN_POOLS`, absent from `KNOWN_ROLES`, invisible on the settings page for
  months).

### 2. Transport: this one earns its own message pair

The standing rule is to reuse `workbench_request` / `workbench_result` — that is
why `generate` needed zero worker changes. **It does not fit here.**
`run_workbench_job` is hard-wired to one `provider.chat` returning a string; an
un-upgraded worker receiving an image job would answer with prose where the
caller expects PNG bytes. This is the documented exception (the same one that
applies to embeddings), so a new pair is correct:

```python
class ImageRequestMsg(BaseModel):          # orchestrator → worker
    type: Literal["image_request"] = "image_request"
    job_id: uuid.UUID
    workflow: str                  # named template the worker ships, e.g. "sdxl_txt2img"
    graph: dict[str, Any] | None = None   # escape hatch: raw ComfyUI graph, overrides `workflow`
    prompt: str
    negative_prompt: str | None = None
    checkpoint: str | None = None  # None → worker default
    width: int = 1024
    height: int = 1024
    steps: int = 30
    cfg: float = 6.0
    sampler: str | None = None
    seed: int | None = None        # None → worker randomizes and reports back
    batch: int = 1                 # capped server-side, see below

class ImageResultMsg(BaseModel):           # worker → orchestrator
    type: Literal["image_result"] = "image_result"
    job_id: uuid.UUID
    index: int                     # one message per image, not one giant frame
    total: int
    success: bool
    image_b64: str = ""            # PNG bytes, base64
    seed: int | None = None
    checkpoint: str | None = None  # what actually ran — the truthful field
    elapsed_ms: int = 0
    error: str | None = None
```

Non-negotiables that come with a new pair:

- **Both `protocol.py` files change in the same commit** (`backend/app/orchestrator/`
  and `worker/app/`). They are a hand-maintained mirror, and a worker validating
  a message the orchestrator can't produce means walking to each machine.
- Add `ImageResultMsg` to the `WorkerInbound` union and to the handler table in
  `routes/workers.py` (alongside `"workbench_result"`).
- Correlate with an `asyncio.Future` keyed by `job_id`, resolved from the WS
  handler, rejected on worker disconnect — the `generate.py` pattern verbatim,
  including a `fail_image_jobs()` on the disconnect path.
- **One message per image.** A 1024² PNG is ~1–2 MB, so ~1.4–2.7 MB base64; a
  batch of four in one frame is ~11 MB. The worker's client is opened with
  `max_size=64 MB`, but the server side has its own limits and a single fat
  frame blocks the socket for everything else on that worker. Cap `batch` at 4
  server-side and stream one result per image.

**Where workflow templates live** is the one design choice worth being explicit
about. Ship a small set of *named* templates inside the worker image
(`sdxl_txt2img`, `flux_schnell_txt2img`) and advertise their names at register.
Then adding a **style** is a parameter change (no redeploy), while adding a
**workflow** touches the Windows box. The `graph` escape hatch exists so a new
workflow can be trialled from the orchestrator without a worker rebuild, and
promoted into a named template once it's proven.

### 3. Data model

`ImageJob`, modelled on `WorkbenchJob` — no git, no DAG, no approval gates:

```
ImageJob
  id            uuid pk
  status        enum  (pending | waking | running | done | error)
  source        enum  (module | api | operator)     -- who asked
  prompt        text
  negative      text null
  params        json          -- {workflow, checkpoint, width, height, steps, cfg, sampler, seed, batch}
  result        json null     -- {images: [{path, thumb, seed, width, height, elapsed_ms}], checkpoint}
  error         text null
  worker_name   varchar null
  conversation_id uuid null   -- set when an Operator tool call made it
  created_at / updated_at
```

Schema arrives through the existing boot path: `create_all` plus an entry in
`_INLINE_MIGRATIONS` for anything added later, and the new enums go in
`_ENUM_MIGRATIONS` (they cannot run inside a transaction).

Reuse `workbench_watchdog_loop`'s shape for a sweep that moves stale
`running`/`waking` jobs to `error` after an orchestrator restart, or the UI will
pin a spinner forever.

### 4. Storage — bytes go on a volume, not in Postgres

`Artifact.content` is `Text`, and PNGs do not belong there. The precedent is
already in the compose file: `repos_data`, a fixed-name named volume.

```yaml
  backend:
    volumes:
      - repos_data:/var/lib/drydock/repos
      - images_data:/var/lib/drydock/images

volumes:
  images_data:
    name: dry-dock-images
```

Layout `/var/lib/drydock/images/<job_id>/<index>.png` plus a `<index>_thumb.webp`
generated on receipt. `beachhead.json` needs no change — `backend` is not a
stateful service; the **fixed volume name** is what survives a blue/green swap,
which is the same bet `repos_data` already makes. Worth confirming against the
`beachhead` skill before the first deploy, since it's the one claim here that
depends on deploy behaviour rather than on this repo.

Add a retention setting (`IMAGE_RETENTION_DAYS`, default 0 = keep) before this
volume becomes the reason a disk fills.

---

## The API has to be asynchronous — and that's forced

`/api/v1/generate` is blocking because a local 32B answers in seconds to tens of
seconds. Darkroom's budget does not fit that shape:

| Stage | Warm | Cold |
|---|---|---|
| Wake + boot + Docker + WS register | — | 45–120 s |
| Checkpoint load | — | 10–30 s |
| Generation (SDXL, 1024², 30 steps) | 5–20 s | 5–20 s |
| **Total** | **~5–20 s** | **~60–170 s** |

`GENERATE_TIMEOUT_SECONDS` defaults to 120 and caps requested timeouts silently.
A blocking image endpoint would therefore fail *exactly* in the cold case that
is most common — the machine is asleep precisely because nobody has asked it for
anything. So:

```
POST   /api/v1/image            → 202 {job_id, status, poll_url}
GET    /api/v1/image/{job_id}   → {status, images:[{url, seed, width, height}], error}
GET    /api/v1/image/{job_id}/file/{index}.png   → the bytes
GET    /api/v1/image/health     → {status, imager_workers, machine, asleep}
```

Notes that matter:

- Router in `backend/app/routes/image.py`, mounted in `main.py` **outside** the
  `dependencies=_auth` block, authenticating with `X-API-Key` /
  `Authorization: Bearer` against `DRYDOCK_API_KEY` — reuse `_require_api_key`
  from `routes/generate.py` (lift it into a shared helper rather than copying;
  the blank-key-means-disabled behaviour is deliberate and must not drift).
- Orchestration in `backend/app/orchestrator/image_jobs.py`, not in the route.
- Optional `wait` parameter (capped, e.g. ≤30 s) that blocks *only* when an
  imager is already online and idle, so a warm caller gets a one-shot
  experience. It must degrade to 202, never to 504.
- **Make `/image/health` truthful.** `/generate/health` returns ok with zero
  workers online, which makes it useless as a readiness gate; don't repeat that.
  Report imager count and whether the machine is asleep.
- Serve bytes through the keyed route rather than a static mount, so the volume
  never becomes a public directory.

### Waking the machine from the API path

Don't loosen the cookie-gated `/api/machines/{name}/wake` — the auth boundary
there is about external clients, not internal orchestration. Add
`ensure_imager_awake()` in `image_jobs.py` that calls the host-agent directly
(the same client `remote_machines.py` already uses), then polls the registry for
an imager to appear, bounded by `IMAGE_WAKE_TIMEOUT_SECONDS` (default 180). Both
the module and the API call it; the job sits in `waking` while it runs, so the
UI can say "waking the Windows box" instead of showing a stalled spinner.

If the wake budget expires, fail the job with a message that names the machine.
"No worker in that pool" is usually "asleep", and the error text should say so.

---

## Module UI

A fifth entry in the `MODULES` list in `routes/dashboard.py` (`darkroom`,
`/darkroom`, accent `rose`), server-rendered with Jinja + HTMX like every other
module.

- **Composer**: prompt, negative prompt, and a collapsed *Advanced* row
  (checkpoint from the worker's advertised list, size preset, steps, CFG,
  sampler, seed with a lock toggle, batch 1–4).
- **Machine strip**: imager online / asleep, with the existing wake button
  wired to the route the dashboard already uses.
- **Job card**: polls its own row every 2 s via HTMX and swaps in the images
  when done. SSE via `streams.py` is a later upgrade, not a phase-1 need.
- **Gallery**: reverse-chronological thumbnails; click for full size, the exact
  parameters that produced it, and two actions — *re-roll* (same params, new
  seed) and *tweak* (params back into the composer). Storing `params` as a blob
  is what makes both one-liners.

---

## Two things the text fleet gets for free

**Prompt expansion.** Diffusion models want dense, comma-weighted prompts;
people type sentences. A *Refine* button dispatches the user's line to a text
worker exactly the way `run_generate` does — unpinned, so it lands on the
always-on Mac mini and survives a sleeping MacBook — with a system prompt that
returns `{prompt, negative_prompt}`. No new transport, no worker change, and it
turns "a rusty Rhodes on a beach at dusk" into something SDXL can actually use.

**The Operator tool** (`generate_image`), added to `OPERATOR_TOOLS` in
`tools.py` and executed server-side like `web_search` / `fetch_url`, returning
the usual `(text_for_model, structured_payload)` pair: the text is a short
confirmation with the URL, the payload carries `{job_id, url, thumb, seed}` so
the transcript renders the image inline instead of a blob.

One sharp constraint: **the tool call blocks the worker's chat turn while it
runs.** A cold 150-second render inside a conversation stalls the thread and
pins a text worker for the duration. So offer `generate_image` in the tool list
**only when an imager is online and idle**, and give it a short internal timeout
that returns "queued — it'll appear in Darkroom" rather than holding the turn.
Building the tool list per-turn is a small change to where `OPERATOR_TOOLS` is
read in `chat.py`.

---

## Phasing

**Phase 1 — the pipeline (~2–3 days)**
ComfyUI on Windows with one SDXL checkpoint and two named workflows. Worker
ComfyUI provider + `capabilities` on `RegisterMsg`. Both `protocol.py` mirrors,
the `ImageRequestMsg`/`ImageResultMsg` pair, `WorkerInbound`, the WS handler
entry. `ImageJob` table, `image_jobs.py` with the Future correlation, disconnect
rejection, and the watchdog. `images_data` volume. Darkroom module page with
composer + gallery. Cookie-auth only; the wake button already exists.

**Phase 2 — the API (~1 day)**
`ensure_imager_awake()`. `routes/image.py` with submit / poll / file / health,
outside the auth block. `IMAGE-API.md` at the repo root in the same shape as
`DRYDOCK-API.md`, carrying a Node and a Python client written for the async
contract, so it drops straight into Gearline.

**Phase 3 — the fleet's own leverage (~1 day)**
Prompt refinement via the text pools. `generate_image` in the Operator tool set,
gated on imager availability, with inline rendering in the transcript.

**Phase 4 — out of scope here, but the door is left open**
img2img, inpainting and background replacement for listing photos. The transport
carries it already: add an `init_image_b64` + `mask_b64` to `ImageRequestMsg`
and a named workflow. That is where this module meets Gearline, and it should be
specced separately once phase 1 proves the path.

---

## Risks & honest caveats

- **VRAM contention with the text workers on the same box** is the most likely
  source of "why is everything slow" once this ships. Decide the priority
  question deliberately rather than discovering it.
- **A sleeping machine makes every latency claim bimodal.** Anything that
  displays a duration should distinguish waiting-for-the-machine from
  generating, or the module will read as broken.
- **ComfyUI's API is a workflow graph, not a prompt field.** The named-template
  approach contains that, but it means the worker and the orchestrator share a
  contract about which node ids carry the prompt. Keep that mapping in the
  template file next to the graph, not in Python.
- **New WS message pair = a hand redeploy of every worker, per machine.** It's
  the right call here, but it is the expensive kind of change; batch any other
  protocol work into the same commit.
- **Anyone holding `DRYDOCK_API_KEY` can spend the GPU.** Single-tenant is the
  design, but a daily cap is cheap — `WEB_SEARCH_DAILY_BUDGET` is the precedent
  to copy.
- **Disk grows quietly.** Retention setting before, not after.

---

## What I'd build first

Stand ComfyUI up on the Windows box by hand and render one image through its own
UI. Then write the smallest possible imager worker — register, receive one
`image_request`, return one `image_result` — and prove the round trip end to end
with a `curl` against a throwaway route, before any of the module UI exists. The
whole risk of this spec sits in that round trip: a second inference substrate,
a new message pair, and a machine that has to be awake. Everything after it is
the same HTMX-and-Postgres work the other four modules already demonstrate.

---

## Open questions

1. **Checkpoint set.** SDXL alone for phase 1, or SDXL + Flux-schnell from the
   start? Two resident checkpoints in 16 GB alongside Ollama is the tight case.
2. **Does the Windows box stay the only imager?** If a second GPU ever appears,
   the direct registry pick needs the same idle-preference logic `_pick_worker`
   has, and no more.
3. **Should Darkroom auto-shutdown the box** when idle for N minutes, or leave
   that to whatever puts it to sleep today?
4. **Naming.** Darkroom fits the shipyard family and says "images" without
   saying "AI". Studio and Foundry are the alternatives.
