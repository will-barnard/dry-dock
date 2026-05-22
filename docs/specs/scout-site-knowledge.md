# Scout — a site-knowledge module

**Status:** proposed
**Author:** Will + assistant pair
**Depends on:** Operator tools mode (web_search + fetch_url), already shipped.

---

## The problem this solves

`fetch_url` today does *generic* extraction: pull JSON-LD, OpenGraph/price meta, and visible text out of whatever HTML comes back. That's fine for static, server-rendered pages and mediocre-to-useless for client-rendered marketplaces like Reverb, where the price you want is injected by JavaScript *after* the HTML we read.

There are only two ways to read those pages:

1. Render the JavaScript every time (headless browser) — reliable but heavy and slow on every fetch.
2. **Learn, once, *where* a given site keeps its data, store that as a reusable recipe, then extract cheaply forever after.**

Option 2 is the whole idea of Scout. Most sites that hide prices behind JS still ship the data *somewhere* in the initial response — a JSON-LD `Product`/`Offer` block, an embedded state blob (`__NEXT_DATA__`, `__NUXT__`, a Redux dump), or an internal JSON API the page calls on load. You only have to find it once. After that, fetching a Reverb listing is "parse this known JSON path," not "render a browser."

So Scout is a module that, per domain, learns and stores **how to read that site** — and the Operator's `fetch_url` becomes *site-aware*, consulting Scout before falling back to generic extraction.

---

## Core concept: site profiles + extraction recipes

A **SiteProfile** is everything Scout knows about one domain (`reverb.com`). Its heart is one or more **ExtractionRecipes** — a versioned, validated description of where the data lives and how to pull it.

A recipe has a **strategy**:

- `jsonld` — parse a JSON-LD block, map fields from it (Reverb's likely win).
- `embedded_json` — pull a script blob (`__NEXT_DATA__` etc.) and read a dotted path (`props.pageProps.listing.price.amount`).
- `selectors` — CSS/XPath selectors for each field (fragile; last resort).
- `api` — the page is backed by a JSON endpoint; store the URL pattern and call it directly (cheapest + most robust when it exists).

…a **field map** (`{price, currency, title, condition, year, seller, url}` → where each lives under the chosen strategy), a **search strategy** (how to turn a query into a results URL or API call for that site), a **rendering hint** (`needs_js: bool` — did we need a browser to even see the data?), and **health** (`last_validated_at`, `success_count`, `failure_count`, `confidence`).

The principle: **a recipe captured once turns an expensive, unreliable fetch into a cheap, structured one.**

---

## Where it plugs into the existing system

Minimal, and it rides patterns already in the codebase:

- **A new module** on the homepage picker, alongside Engineer / Operator / Workbench. Working name **Scout** (alternatives: Atlas, Sources).
- **`web_fetch.fetch(url)` becomes site-aware.** Before generic extraction it looks up the domain's active recipe; if one exists, it applies the recipe and returns clean structured fields (great for the model). No recipe → today's generic extraction, and optionally enqueue a background learning job for that domain.
- **Learning runs as jobs on the worker fleet**, mirroring the `WorkbenchJob` non-streaming dispatch pattern exactly (`SiteLearningJob`, dispatched to the docs/researcher pool, result handler applies the proposed recipe). Nothing new in the transport layer.
- **Storage** is just two more tables + the inline-migration approach already used in `main.py`.
- **The Operator benefits for free**: when `fetch_url` hits a Scout-known domain, the model gets crisp `price: $X, condition: Y` fields instead of a wall of page text — which directly fixes the "weak synthesis / too shallow" complaints, because the model spends its budget reasoning instead of scraping.

---

## Data model

```
SiteProfile
  domain            text  (unique, e.g. "reverb.com")
  display_name      text
  status            enum  (learning | active | stale | failed)
  notes             text
  created_at / updated_at

ExtractionRecipe
  id                uuid
  site_profile_id   fk
  version           int
  strategy          enum  (jsonld | embedded_json | selectors | api)
  field_map         json  ({"price": "...", "title": "...", ...})
  search_strategy   json  ({"type": "url", "pattern": "https://reverb.com/marketplace?query={q}"})
  needs_js          bool
  confidence        float
  active            bool   (one active recipe per profile)
  last_validated_at timestamptz
  success_count / failure_count int
  created_at

SiteLearningJob   (mirrors WorkbenchJob)
  id / kind / status / input / result / error / worker_name / timestamps
```

Recipes are **versioned and validated before activation** — re-learning a site never clobbers a working recipe until the new one proves itself on sample pages.

---

## The learning pipeline

A `SiteLearningJob` for a domain, given one or more sample URLs:

1. **Acquire sample pages.** Static fetch first. If the fields we're after aren't present in static HTML, this is the one place we reach for a **headless browser (Playwright)** to render the page and *observe* how it loads data — including the network calls it makes. This is the heavy step, and it only runs at *learn* time, occasionally — not on every fetch.
2. **Reduce + analyze.** Hand the model the candidate signal — JSON-LD blocks, any embedded-state script blobs, observed API responses, a few candidate selectors — and ask it to produce an extraction recipe as JSON: which strategy, and where each field lives.
3. **Validate.** Apply the proposed recipe to the sample page(s). Sanity-check the output (price parses as a number, title is non-empty, etc.). Only a recipe that populates the expected fields gets activated.
4. **Store** as a new recipe version; flip it active; record `needs_js` and confidence.

The elegant payoff: if learning discovers Reverb is backed by an API or an embedded JSON path, the stored recipe is `api` or `embedded_json` — and runtime fetches never need the browser again. The browser cost is amortized to near-zero.

**Triggering:** manual (you add `reverb.com` + paste a couple of listing URLs) for Phase B; automatic (Operator fetched an unknown domain → enqueue a low-priority learning job) for Phase C.

**Staleness:** sites change and recipes rot. Track `failure_count` at fetch time; when a recipe starts failing, mark the profile `stale` and auto-enqueue a re-learn. The versioning means the old recipe keeps serving until the new one validates.

---

## Module UI

A Scout page that lists known domains with status chips (active / learning / stale / failed) and health (success/failure counts, last validated). Per profile:

- The learned recipe rendered human-readably ("Price: JSON-LD → offers.price; needs JS: no").
- A **Test fetch** box — paste a URL, see exactly what Scout extracts, before trusting it.
- **Re-learn** and **add sample URLs** buttons.
- Add-a-domain form (domain + sample URLs → kicks a learning job, with a status page like the Workbench import flow).

---

## Phasing

- **Phase A — site-aware runtime, hand-written recipes.** Data model + `fetch_url` consults SiteProfile + a recipe applier (jsonld / embedded_json / selectors / api). Author the Reverb recipe by hand to prove the runtime path end to end. ~1–2 days. *This alone probably fixes your Reverb fetches*, since you'd hand-write the JSON-LD/embedded-JSON path once.
- **Phase B — agent-assisted learning (static).** SiteLearningJob: static fetch → model proposes recipe → validate → store. The Scout UI to add/inspect/test profiles. ~3 days.
- **Phase C — headless discovery + autonomy.** Playwright for JS-heavy sites at learn time, auto-trigger learning on unknown domains, staleness detection + auto re-learn. ~1 week, and the Playwright dependency lands here, isolated to the learning worker.

---

## Risks & honest caveats

- **Maintenance surface.** This is a real subsystem. It's worth it precisely because you fetch the *same* sites repeatedly (Reverb, a few others) — the recipe amortizes. For one-off fetches across the long tail of the web, generic extraction is still the right tool; Scout is for your high-value recurring sources.
- **Recipes rot.** Mitigated by health tracking + versioned re-learn, but expect occasional drift.
- **Headless browser weight.** Kept out of the hot path by design — it's a learn-time tool only, and ideally a separate worker/pool so its Chromium footprint doesn't bloat every worker image.
- **ToS / robots.** Same posture as `fetch_url` — respect robots, identify honestly, rate-limit. An official API (if one ever returns) always beats a learned recipe; Scout's `api` strategy is how you'd slot one in.
- **Validation is load-bearing.** A model-proposed recipe that isn't validated against real pages will silently emit garbage. The activation gate is non-negotiable.

---

## What I'd build first

Phase A, hand-authoring the Reverb recipe. It's the smallest slice that delivers the thing you actually want — reliable Reverb prices — and it forces the runtime architecture (site lookup + recipe applier + structured output into the Operator) into place. Learning (Phase B) then becomes "automate writing the recipe we just proved by hand," which is a much safer thing to build second than first.
