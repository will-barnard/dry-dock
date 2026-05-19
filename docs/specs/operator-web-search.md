# Operator web search — design spec

**Status:** proposed
**Author:** Will + assistant pair
**Scope:** add an optional "browse the web" toggle to the Operator chat module so local-model answers don't trail off into 2023-era knowledge.

---

## Problem

The Operator chat ships with Qwen 2.5, Llama 3.1, Mistral, and friends — all of which have knowledge cutoffs from 6–18 months ago. For factual questions ("what's the current version of X", "who won Y", "what changed in Z's API last quarter") the model either confabulates or apologises. The fix is to let the orchestrator fetch live web results and feed them to the model as context.

Goals:

1. **Off by default, on by toggle.** A checkbox in the chat composer (and a sticky per-conversation setting) lets the user enable it.
2. **Visible in the transcript.** Each searched query + the URLs it pulled show up in the conversation log so the user can audit what the model saw.
3. **No per-worker API keys.** Searches run on the orchestrator, not the worker. Workers stay simple; rotating an API key is one place.
4. **Graceful fallback** when the search backend, model, or quota is unavailable — never silently lie.
5. **Cheap.** Personal use should comfortably fit in a free tier.

Non-goals (for now):

- Browser-level access (a la Claude in Chrome). Too heavy for local Ollama on a laptop.
- General tool-calling (file I/O, calculators, MCP). This spec is scoped to web search; the protocol changes are written to be reusable for tools later, but we ship one tool first.
- Workbench integration. Workbench prompts are structured-output one-shots; the value is in chat, where the model gets to decide whether to search.

---

## Two viable architectures

### Approach A — pre-flight injection (no tool calling)

For each user turn where the toggle is on: classify whether the question is time-sensitive (heuristic + a tiny model call, or always-on per the user's preference), run one web search, inject the results as a `system` message above the history, send the whole batch to the model.

```
User → orchestrator
  ├─ heuristic: needs search?
  ├─ if yes: search backend → top N results
  ├─ inject as "Web context:\n[1] title — url\n   snippet\n[2] …" system msg
  └─ worker chat (one turn, no extra round-trips)
→ stream back, done.
```

**Pros:** works with *every* local model. No tool-calling support needed. Minimal protocol changes (just a richer `messages` array). One extra hop, predictable latency.
**Cons:** model can't "decide" to skip the search or run a follow-up. Search query is whatever the user typed, not what the model would have asked.

### Approach B — model-driven tool calling (proper)

The model gets a `web_search(query: str)` tool. It emits a `tool_call` chunk; orchestrator runs the search and pipes results back; model continues. Cap at 3 iterations per turn.

```
User → worker
        ↓
     model emits {tool_call: web_search, args: {query: "…"}}
        ↓
     worker → orchestrator (tool_call_msg)
        ↓
     orchestrator runs search
        ↓
     orchestrator → worker (tool_result_msg)
        ↓
     model continues, emits content (or another tool_call)
        ↓
     chat_done
```

**Pros:** the model picks the right query, can chain a calculator-style follow-up, and learns when *not* to search. Honest UI: "the model searched for X" rather than "we searched for whatever you typed."
**Cons:** requires a tool-capable model. Of the user's current Ollama lineup, only `qwen2.5:14b` and `llama3.1:8b` reliably support tool calling. Multi-iteration loops add latency. Worker protocol gets a new message type.

### Recommendation: ship A first, then add B

Phase 1: ship Approach A behind the checkbox. It's a day of work, validates whether the user actually likes the feature, and works on every machine in the fleet.

Phase 2: when we have a tool-capable model assigned to a pool, layer in Approach B. The protocol changes are forward-compatible — Approach A's "inject as system message" path stays the fallback for models that don't support tools.

The rest of this doc specifies both phases. Phase 1 sections are marked **[P1]**, phase 2 sections **[P2]**.

---

## Search backend

| Backend | Pros | Cons | Cost |
|---|---|---|---|
| **Tavily** | Designed for LLM use — returns ranked, snippet-summarised, ready-to-feed results. One API call, no scraping. | Closed source, paid. | $5 / 1k queries (1k/mo free tier) |
| **Brave Search API** | Independent index, decent quality, JSON. | Free tier requires personal use attestation; results are search engine results, need to scrape pages for snippets. | Free 2k/mo |
| **SearXNG self-hosted** | Run on Beachhead, no API keys. Aggregates Google/Bing/DDG so quality is good. | Sysadmin overhead, rate-limit risk from upstream engines. | Free |
| **DuckDuckGo HTML scraping** | No key, no signup. | Brittle HTML scraping, no per-result snippets, fragile in CI. | Free |

**Recommendation: Tavily for the MVP, SearXNG as the upgrade path.**

Tavily's response shape is ideal — it returns `{title, url, content (snippet), score}` per result, plus an optional `answer` field that pre-summarises the top results. For $5/1k queries on personal use the cost is irrelevant. The orchestrator wraps it behind a `WebSearchProvider` interface so swapping in SearXNG later is one file.

```python
# backend/app/orchestrator/web_search.py
class WebSearchProvider(Protocol):
    async def search(self, query: str, *, max_results: int = 5) -> SearchResponse: ...

class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    score: float | None = None

class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    answer: str | None = None      # provider-side summary if available
    elapsed_ms: int
```

Config in `app.config`:

```
WEB_SEARCH_ENABLED=true|false   # global kill switch
WEB_SEARCH_BACKEND=tavily       # tavily | searxng | none
TAVILY_API_KEY=…
SEARXNG_URL=…                   # only for searxng
WEB_SEARCH_MAX_RESULTS=5
WEB_SEARCH_DAILY_BUDGET=200     # safety net per user; 0 = unlimited
```

---

## Protocol additions

### [P1] enrich `ChatRequestMsg`

`backend/app/orchestrator/protocol.py`:

```python
class ChatRequestMsg(BaseModel):
    type: Literal["chat_request"] = "chat_request"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    model: str | None
    messages: list[dict[str, str]]
    # Phase 2 fields, ignored by P1 worker code:
    tools: list[dict] | None = None        # OpenAI-style tool schema
    tool_use: Literal["auto", "none"] = "none"
```

In Phase 1, the orchestrator just stuffs an extra `{"role": "system", "content": "Web context:\n…"}` into `messages` before sending. The worker doesn't need to know anything about web search.

### [P2] new messages

```python
class ChatToolCallMsg(BaseModel):
    """worker → orchestrator: the model wants to call a tool."""
    type: Literal["chat_tool_call"] = "chat_tool_call"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    tool_call_id: str                       # echoed back in ChatToolResultMsg
    name: Literal["web_search"]             # extensible later
    arguments: dict                         # {"query": "…", "max_results": 5}


class ChatToolResultMsg(BaseModel):
    """orchestrator → worker: result of the tool call."""
    type: Literal["chat_tool_result"] = "chat_tool_result"
    conversation_id: uuid.UUID
    assistant_message_id: uuid.UUID
    tool_call_id: str
    success: bool
    content: str                            # JSON-encoded tool output
    error: str | None = None
```

A tool round-trip is *within* one assistant turn. The worker emits `chat_chunk` deltas as normal, plus zero or more `chat_tool_call`s, and finishes with `chat_done`. The orchestrator interleaves `chat_tool_result` sends in response.

---

## Data model

### [P1] `Conversation.web_search_enabled`

```sql
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS web_search_enabled BOOLEAN NOT NULL DEFAULT FALSE;
```

Sticky per-conversation. The checkbox in the composer toggles this. New conversations default to whatever the global default in Settings says (also a future addition).

### [P1] new `MessageRole.TOOL` + tool messages

```python
class MessageRole(str, enum.Enum):
    SYSTEM    = "system"
    USER      = "user"
    ASSISTANT = "assistant"
    TOOL      = "tool"          # NEW
```

A "tool" message stores:
- `role = TOOL`
- `content` = the JSON-encoded `SearchResponse` (or whatever the future tool returned)
- `tool_name` = `"web_search"`
- `tool_call_query` = the query string (for UI display)

Two more columns on `ConversationMessage`:

```sql
ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS tool_name VARCHAR(64);
ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS tool_payload JSON;
```

In Phase 1 the orchestrator persists *one* TOOL message per turn (the pre-flight search). In Phase 2 it can persist N per turn (one per tool call the model emits).

### Budget tracking

A tiny `web_search_usage` table — `(user_id, date, count)` — increments per search. Used to enforce `WEB_SEARCH_DAILY_BUDGET` and surface "you've used 12 / 200 searches today" in the UI.

---

## UX

### Composer

The Operator chat composer gets a single checkbox under the textarea:

```
[ Send message ─────────────────────── ]
[ ] Search the web for this answer    Tavily · 12 / 200 today
```

State rules:
- Checkbox reflects `conversation.web_search_enabled` on load.
- Toggling it POSTs to a new `/operator/conversations/{id}/settings` route and updates the row (one round-trip, no JS framework needed).
- When checked, every turn in this conversation runs through the search path until unchecked.

### Mid-stream indicator

While a search is in flight, the assistant-message div renders a small badge above the streaming text:

```
🌐 Searching: "next.js 15 server actions changes"
```

The orchestrator publishes a `tool_status` event on the conversation topic at the start and end of each search. The SSE handler swaps the badge to:

```
🌐 Searched: 5 results (Tavily, 480 ms)
  ▸ Next.js 15 release notes — nextjs.org/blog/...
  ▸ …
```

Each URL is clickable and opens in a new tab. The list is collapsible.

### Citations in the model output

The orchestrator's pre-flight injection includes a prompt like:

```
Web context — use these results to answer the user's question. Cite them
inline with [1], [2], etc. Don't invent details beyond what the snippets say.
If the snippets don't answer the question, say so.

[1] {title}
    {url}
    {snippet}

[2] …
```

The assistant's reply is rendered with `[1]`, `[2]` as anchored superscripts that link to the corresponding tool message's URL list.

### Visibility in the transcript

Tool messages render in the conversation as collapsed cards between the user message and the assistant reply. Once collapsed they show just `🌐 Searched: "…" — 5 results`. Expanded, they show the full list of results with snippets.

This means the user can scroll up a long thread and see exactly what each turn searched for. Important for trust.

---

## Failure modes

| Condition | Behaviour |
|---|---|
| `WEB_SEARCH_ENABLED=false` globally but a user toggles it | Composer disables the checkbox with a tooltip "web search is disabled in settings." |
| Backend down / timeout (>5s) | Pre-flight: skip injection, prepend a system note "(web search unavailable)". Turn proceeds with model-only knowledge. Phase 2: send a `chat_tool_result` with `success=false, error="…"`. |
| Quota exhausted | Same as backend-down, with the tooltip on the checkbox showing "12 / 200 today — quota reached." |
| Pool's model doesn't support tools (Phase 2) | Fall back to Phase 1 path (pre-flight injection). Logged but invisible to the user. |
| Search returns 0 results | Inject the empty-result message: `"Web search returned no results for: <query>."` Model is told to answer from its own knowledge with a caveat. |
| Tavily API key missing | Web search globally unavailable; composer checkbox is hidden entirely. |

---

## Phasing

### Phase 1 — pre-flight injection (~1 day)

1. `backend/app/orchestrator/web_search.py` — `WebSearchProvider`, `TavilyProvider`, `SearchResponse` shape.
2. `backend/app/config.py` — env vars + `Settings` fields.
3. `backend/app/models.py` — `web_search_enabled` on Conversation, `tool_name` + `tool_payload` on ConversationMessage, `MessageRole.TOOL`, `WebSearchUsage` table.
4. `backend/app/main.py` — 4 new `_INLINE_MIGRATIONS` entries; one `_ENUM_MIGRATIONS` entry for the new role.
5. `backend/app/orchestrator/chat.py` — `dispatch_turn` checks `conversation.web_search_enabled`, runs the search via the provider, persists a TOOL message, injects a system message, publishes the `tool_status` events.
6. `backend/app/routes/operator.py` — new `POST /operator/conversations/{id}/settings` route; the message-send route reads the new field.
7. `backend/app/templates/operator.html` — checkbox + tool-message render block.
8. `frontend/operator.js` (or inline) — render the searching/searched badge from SSE events.

### Phase 2 — model-driven tool calls (~3 days)

1. `worker/app/protocol.py` — `ChatToolCallMsg`, `ChatToolResultMsg`; `ChatRequestMsg` accepts `tools`.
2. `worker/app/providers/ollama.py` — pass `tools` to Ollama, parse `tool_calls` from streamed response.
3. `worker/app/main.py` — when the model emits a tool_call, send `chat_tool_call`, wait for `chat_tool_result`, feed it back to the running `provider.chat` continuation.
4. `backend/app/orchestrator/chat.py` — handler for `chat_tool_call`: run the search, send `chat_tool_result`. Cap at 3 iterations per turn.
5. Pool/model capability flag (`supports_tools: bool`) — for the orchestrator to decide which path to take.

---

## Open questions

1. **Heuristic vs. always-on in Phase 1.** The checkbox says "search for this answer." Should the orchestrator search on every turn while the checkbox is on, or use a tiny classifier (regex for "latest/current/today" + dates) to decide? I lean **always-on while checked** for predictability — the user already gave us consent by checking the box. The classifier adds complexity and a failure mode (false negatives → "why didn't it search?").

2. **Query rewriting.** The user's literal message ("hey, do you know what's new in next.js 15?") is verbose. A 50-token rewrite step (with a tiny local model) could turn it into "next.js 15 changes" and improve search quality. Probably worth it in Phase 1; cheap and quality-boosting.

3. **Stale-while-revalidate caching.** Cache `(query → SearchResponse)` for 10 minutes? Saves cost on repeated queries in the same thread. Trivial to add.

4. **Privacy / outbound network policy.** Searches go to Tavily by default. Should the per-conversation settings expose the backend choice (so a paranoid user can pick SearXNG self-hosted)? Maybe in a v2; for now, global Settings.

---

## Test plan

- **Unit:** `TavilyProvider.search` against a recorded VCR fixture; budget enforcement.
- **Integration:** end-to-end with a stub provider — user toggles checkbox, sends "what's the latest version of Bun", asserts a TOOL message lands, asserts the system-injected context shows up in `dispatch_turn`'s outgoing `messages`.
- **Manual smoke:** with a real Tavily key on a worker running `qwen2.5:14b`, ask "who won the 2025 Champions League final" (or any post-cutoff event). Verify cited results in the assistant reply, verify the searched badge renders, verify the conversation reload still shows the tool card.

---

## Summary

The minimum viable cut is: **a checkbox, one new table column, one new env var, one new orchestrator module, a small template change, and a pre-flight search injected as a system message.** That's it for Phase 1 — no worker changes, no protocol changes, works on every model the user has installed. Phase 2 layers proper tool-calling on top for models that support it, with the Phase 1 path remaining the fallback.
