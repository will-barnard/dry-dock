# Pilot

Pilot is the chat module (formerly **Operator**). It asks the user one
question — how much thought should this get — and resolves everything else
from configuration.

```
Lightweight  → fast, always-on machine, no tools
Thoughtful   → the big model, and the only mode that can use tools
```

Pools, model tags, and the search-vs-tools mechanism are no longer chat UI.
They live on `/pilot/settings`.

## Late binding is the point

A conversation row stores **only its mode**. `chat.dispatch_turn` resolves
that mode to a concrete `(pool, model)` pair on every turn, via
`orchestrator/pilot.py`.

The old Operator wrote `conversation.pool` and `conversation.model` once, at
creation, and offered no way to change them afterwards. Retiring a model
therefore bricked every thread pinned to it — permanently, since the only
repair was editing Postgres by hand. Late binding removes that failure class
and adds two conveniences:

- editing a mode in settings heals every existing thread, including ones whose
  last turn failed;
- a thread can switch modes mid-conversation, keeping its history.

The legacy `pool` / `model` columns are still on the table but are never read.
They're kept so an old thread's original pinning stays inspectable.

## Configuration

Stored in `app_settings` under `pilot.<mode>.{pool,model,tools}`, read through
`settings_service.get_raw` / `set_raw` (the `pilot.` prefix is registered in
`_CACHED_PREFIXES`, so reads hit the same cache as role pins).

| Setting | Meaning |
|---|---|
| `pool` | which pool answers — pools map one-to-one to machines |
| `model` | an exact Ollama tag, or **blank** for each worker's own `DEFAULT_MODEL` |
| `tools` | may this mode call tools? Forced off for anything but Thoughtful |

Blank is the safest model setting: a worker can never 404 on its own
configured default.

### Where the model list comes from

The dropdown is the **union of `installed_models` across the pool's online
workers**. That value originates on the machine itself: the worker calls
Ollama's `/api/tags` (what `ollama list` shows) during `register()` and ships
the result in `RegisterMsg`.

Two consequences:

- **It's a snapshot, not a live query.** Nothing refreshes `installed_models`
  except a re-register, so a freshly pulled model doesn't appear until that
  worker reconnects (`./workers.sh restart`), and a deleted one lingers until
  the same. A worker whose Ollama was unreachable at startup registers with an
  **empty** list — it then fails every pinned model, and the settings page
  calls that out by name.
- **Union ≠ every worker.** In a pool spanning two machines — `reviewer`,
  `tester` and `validator` all do — the list can offer a tag only one of them
  has. So a pinned model is treated as a hard requirement at dispatch:
  `dispatch_turn` filters the pool to workers that actually have it, and says
  so plainly if none do. Partial coverage is legal but costs capacity, and the
  settings page says "installed on 1 of 3 online workers" rather than a
  reassuring green tick.

### Making the choice obvious

A flat list of Ollama tags asks the user to know things the app already knows —
that `llama3` and `llama3.1` are different models with different tool support,
that a 70B answer is minutes rather than seconds, that a tag the pool offers may
live on only one of its machines. So `rank_models` annotates, groups and orders
them per mode, and the template renders that as `<optgroup>`s:

| Group | Meaning |
|---|---|
| **Best for \<mode\>** | on every online worker, and able to do what the mode needs |
| **Works, but can't use tools** | Thoughtful only — it would silently degrade to search |
| **Not installed on every worker** | usable, but it costs pool capacity |

Each option carries its own facts: `qwen2.5-coder:32b — 32B · tools · code-tuned`.
Size is parsed from the tag; `:latest` reads as "size unknown" rather than being
guessed, and sorts last within its group.

The ★ suggestion is the top of the first group, with one-click apply:

- **Thoughtful** — the largest tool-capable model every worker has. Size wins over
  general-vs-code here, since a large code-tuned model still reasons better than a
  small general one.
- **Lightweight** — the fastest *general-purpose* model every worker has. Code
  models are demoted below any general alternative for both modes: Pilot is a
  chat surface, and "smallest" on its own would happily recommend a 6.7B
  fill-in-the-middle code model as a conversationalist.

Pools are labelled by the hardware behind them (`coder — macbook · 64 GB · 1
online`) for the same reason — a pool name is an implementation detail; the
machine is the actual choice.

### This is NOT the fleet Settings page

`/settings` role→model pins are a **hard filter applied by the task router**
(`router.py::_worker_compatible`) — a worker without that exact tag is dropped
from Engineer dispatch before priority is considered. Pilot chat never goes
through the router; `dispatch_turn` reads the registry directly. The two pages
look alike and do entirely different things.

## Web access

The conversation stores intent (`web_mode` = `off` | `on`). The mechanism is
derived per turn by `pilot.web_mechanism`:

| Mode | Web on | Mechanism |
|---|---|---|
| Lightweight | yes | `search` — pre-flight SearXNG injection, works on any model |
| Thoughtful (tools on, capable model) | yes | `tools` — agentic `web_search` / `fetch_url` loop |
| Thoughtful (tools on, model not tool-capable) | yes | `search` — deliberate degrade |

That last row is the important one. **Ollama silently ignores a `tools` field
on a model that can't call tools**: the request succeeds and the model answers
from memory, so the thread looks researched while being anything but. Degrading
to `search` turns a silent wrong answer into a visibly weaker one.

Tool capability is checked against a family allowlist
(`pilot.TOOL_CAPABLE_PREFIXES`). That's a knowingly cheap stand-in. The
truthful version is to have the worker call Ollama's `/api/show` per installed
model at register and report a `tool_capable` list on `RegisterMsg` — additive
on the message, so old workers degrade fine, but it is a worker change and
therefore a by-hand walk of every machine. Worth folding into the next fleet
update; until then the allowlist plus the warning on the settings page.

## Diagnosis

Hiding the model is only safe if the truth stays reachable:

- every assistant turn records `worker_name` **and** `model_used`, rendered
  under the message;
- the thread header's tooltip names the resolved pool and model;
- `/pilot/settings` states, per mode, whether any worker is online, whether the
  pinned model is installed on one, and whether it can call tools;
- the composer warns about an empty pool *before* a message is typed, and
  offers the other mode in one click;
- `chat._humanize_error` translates the failures that actually happen — an
  Ollama 404 becomes "*model* isn't installed on *worker*", not an httpx URL.

## URLs

`/operator/*` 307-redirects to `/pilot/*`, and `/stream/operator/{id}` is kept
as an alias of `/stream/pilot/{id}` so a tab left open across a deploy keeps
streaming. The `ImageSource.OPERATOR` enum value is deliberately unchanged:
PostgreSQL can't drop enum values, and renaming it would buy nothing.
