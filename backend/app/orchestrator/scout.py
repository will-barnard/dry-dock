"""Scout — site-knowledge module (Phase A).

Stores, per domain, a validated "recipe" for extracting structured fields
from that site, so the Operator's fetch_url can return clean fields (price,
title, condition) instead of a wall of page text. Phase A is the runtime
path + hand-written recipes; agent-assisted learning is Phase B.

This module owns:
  - the recipe applier (apply_recipe) used by web_fetch
  - profile lookup helpers
  - the idempotent Reverb starter seed
"""
from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import structlog
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import (
    ExtractionRecipe,
    ExtractionStrategy,
    SiteLearningJob,
    SiteProfile,
    SiteProfileStatus,
)

log = structlog.get_logger()

# Pools to try for a Scout learning inference, in order.
_LEARN_POOLS = ("docs", "researcher", "planner", "coder")

# Fields we try to surface, in display order.
KNOWN_FIELDS = ("title", "price", "currency", "condition", "year", "seller", "url", "description")

# Common containers for embedded-JSON state blobs.
_EMBEDDED_CONTAINERS = ("__NEXT_DATA__", "__NUXT__", "__APOLLO_STATE__")


# ── helpers ─────────────────────────────────────────────────────────


def domain_of(url: str) -> str | None:
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _resolve_path(obj: Any, dotted: str) -> Any:
    """Walk a dotted path through nested dicts/lists. A list step takes the
    first element (marketplace 'offers' is often a one-element list)."""
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, list):
            cur = cur[0] if cur else None
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    # If we land on a list, take the first scalar-ish element.
    if isinstance(cur, list):
        cur = cur[0] if cur else None
    return cur


def _iter_jsonld_objects(soup: BeautifulSoup):
    """Yield every JSON-LD object on the page, flattening @graph arrays."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                if "@graph" in item and isinstance(item["@graph"], list):
                    for g in item["@graph"]:
                        if isinstance(g, dict):
                            yield g
                else:
                    yield item


def _find_embedded_json(soup: BeautifulSoup) -> dict | None:
    """Find a likely embedded-state JSON blob (__NEXT_DATA__ etc.)."""
    # Framework script ids first.
    for cid in _EMBEDDED_CONTAINERS:
        tag = soup.find("script", id=cid) or soup.find("script", attrs={"name": cid})
        if tag and tag.string:
            try:
                return json.loads(tag.string)
            except (json.JSONDecodeError, ValueError):
                pass
    # Any application/json script as a fallback.
    for tag in soup.find_all("script", attrs={"type": "application/json"}):
        if tag.string:
            try:
                parsed = json.loads(tag.string)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                continue
    return None


_SELECTOR_ATTR_RE = re.compile(r"::attr\(\s*([\w-]+)\s*\)\s*$")
_SELECTOR_TEXT_RE = re.compile(r"::text\s*$")


def _normalize_selector(raw: str) -> str:
    """Normalize the dozen syntactic variants models emit for the same
    selector. Canonicalize to `<css>` or `<css>::text` or `<css>::attr(name)`.

    Examples handled:
        "span.year :: text"            → "span.year::text"
        "h1[itemprop='name']:: text"   → "h1[itemprop='name']::text"
        "meta[property='og:url']@attr['content']" → "meta[property='og:url']::attr(content)"
        "a.link:attr(href)"            → "a.link::attr(href)"
        "div.x ::attr( content )"      → "div.x::attr(content)"
    """
    s = raw.strip()
    # `@attr['x']` / `@attr[x]` / `@attr("x")` → `::attr(x)`
    s = re.sub(
        r"@attr\s*\[\s*[\"']?([\w-]+)[\"']?\s*\]", r"::attr(\1)", s
    )
    s = re.sub(
        r"@attr\s*\(\s*[\"']?([\w-]+)[\"']?\s*\)", r"::attr(\1)", s
    )
    # Single-colon `:attr(x)` → `::attr(x)`. Negative-lookbehind to avoid
    # touching an already-double-colon form.
    s = re.sub(r"(?<!:):attr\(", "::attr(", s)
    # Collapse whitespace around `::` (" :: text" → "::text").
    s = re.sub(r"\s*::\s*", "::", s)
    # Tighten whitespace inside ::attr(...).
    s = re.sub(r"::attr\(\s*([\w-]+)\s*\)", r"::attr(\1)", s)
    # A trailing bare " text" word after a CSS selector means "::text".
    s = re.sub(r"\s+text\s*$", "::text", s) if not s.endswith("::text") else s
    return s.strip()


def _apply_selector(soup: BeautifulSoup, raw_selector: str) -> str | None:
    """Resolve one selector against the page, tolerant of the syntactic
    variants models commonly emit (see _normalize_selector)."""
    if not isinstance(raw_selector, str) or not raw_selector.strip():
        return None
    selector = _normalize_selector(raw_selector)
    attr: str | None = None
    m = _SELECTOR_ATTR_RE.search(selector)
    if m:
        attr = m.group(1).strip()
        selector = selector[: m.start()].strip()
    else:
        selector = _SELECTOR_TEXT_RE.sub("", selector).strip()
    if not selector:
        return None
    try:
        el = soup.select_one(selector)
    except Exception:
        return None
    if el is None:
        return None
    if attr:
        val = el.get(attr)
        return val.strip() if isinstance(val, str) else None
    text = el.get_text(strip=True)
    return text or None


def _css_hint(el) -> str:
    """Build a compact, usable CSS selector for an element: tag + id or up to
    two classes. Good enough for the model to target a price node."""
    if el is None or not getattr(el, "name", None):
        return ""
    tag = el.name
    el_id = el.get("id")
    if el_id:
        return f"{tag}#{el_id}"
    classes = [c for c in (el.get("class") or []) if c][:2]
    if classes:
        return tag + "".join(f".{c}" for c in classes)
    return tag


# ── recipe applier ─────────────────────────────────────────────────


def apply_recipe(html: str, recipe: ExtractionRecipe) -> dict[str, Any]:
    """Apply a recipe to page HTML and return whatever it resolves.

    Single shape → {field: value, ...}.
    List shape   → {"items": [{field: value, ...}, ...]}.
    Empty dict / empty items means the recipe matched nothing (caller falls
    back to generic extraction)."""
    soup = BeautifulSoup(html, "html.parser")
    field_map: dict[str, str] = recipe.field_map or {}

    # ── list shape: many rows via a repeating item selector ──
    if getattr(recipe, "result_shape", "single") == "list":
        item_sel = getattr(recipe, "list_item_selector", None)
        if not item_sel:
            return {"items": []}
        try:
            rows = soup.select(item_sel)
        except Exception:
            rows = []
        items: list[dict] = []
        for row in rows:
            rec: dict[str, Any] = {}
            for field, sel in field_map.items():
                val = _apply_selector(row, sel)
                if val:
                    rec[field] = val
            if rec:
                items.append(rec)
        return {"items": items}

    out: dict[str, Any] = {}

    if recipe.strategy == ExtractionStrategy.JSONLD:
        # Prefer a JSON-LD object that actually contains the mapped paths;
        # Product/Offer pages can carry several blocks (BreadcrumbList, etc.).
        best: dict | None = None
        best_hits = 0
        for obj in _iter_jsonld_objects(soup):
            hits = sum(
                1 for path in field_map.values()
                if _resolve_path(obj, path) is not None
            )
            if hits > best_hits:
                best, best_hits = obj, hits
        if best is not None:
            for field, path in field_map.items():
                val = _resolve_path(best, path)
                if val is not None:
                    out[field] = val

    elif recipe.strategy == ExtractionStrategy.EMBEDDED_JSON:
        blob = _find_embedded_json(soup)
        if blob is not None:
            for field, path in field_map.items():
                val = _resolve_path(blob, path)
                if val is not None:
                    out[field] = val

    elif recipe.strategy == ExtractionStrategy.SELECTORS:
        for field, selector in field_map.items():
            val = _apply_selector(soup, selector)
            if val:
                out[field] = val

    # ExtractionStrategy.API isn't applied to HTML — it's a Phase C fetch path.
    # Normalize scalar values to strings for clean display.
    return {k: (v if isinstance(v, (str, int, float)) else json.dumps(v)) for k, v in out.items()}


_PRICE_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d{1,2})?")


def _price_to_float(text: Any) -> float | None:
    m = _PRICE_NUM_RE.search(str(text or ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def format_fields(domain: str, url: str, fields: dict[str, Any]) -> str:
    """Render extracted data as a compact block for the model. Handles both
    the single-record shape and the list shape (with a price-range summary)."""
    # ── list shape ──
    if isinstance(fields, dict) and "items" in fields:
        items = fields.get("items") or []
        lines = [f"Fetched via {domain} recipe (list, {len(items)} items): {url}", ""]
        prices = [p for p in (_price_to_float(it.get("price")) for it in items) if p is not None]
        if prices:
            lines.append(
                f"Price range: ${min(prices):,.0f} – ${max(prices):,.0f} "
                f"(median ${sorted(prices)[len(prices)//2]:,.0f}, {len(prices)} priced)"
            )
            lines.append("")
        for i, it in enumerate(items[:40], 1):
            bits = [str(it[k]) for k in ("title", "price", "condition") if it.get(k)]
            extra = " | ".join(f"{k}={v}" for k, v in it.items()
                                if k not in ("title", "price", "condition"))
            line = f"{i}. " + " — ".join(bits)
            if extra:
                line += f"  ({extra})"
            lines.append(line)
        return "\n".join(lines)

    # ── single shape ──
    lines = [f"Fetched via {domain} recipe: {url}", ""]
    for key in KNOWN_FIELDS:
        if key in fields:
            lines.append(f"{key}: {fields[key]}")
    for key, val in fields.items():
        if key not in KNOWN_FIELDS:
            lines.append(f"{key}: {val}")
    return "\n".join(lines)


def recipe_has_data(fields: dict[str, Any]) -> bool:
    """True if an apply_recipe result actually extracted something."""
    if isinstance(fields, dict) and "items" in fields:
        return bool(fields.get("items"))
    return bool(fields)


def _url_matches(pattern: str | None, url: str) -> bool:
    """A blueprint's url_pattern matches if it's a substring of the URL, or —
    when it contains '*' — an fnmatch glob over the URL."""
    if not pattern:
        return False
    if "*" in pattern:
        import fnmatch
        return fnmatch.fnmatch(url, pattern) or fnmatch.fnmatch(url, f"*{pattern}*")
    return pattern in url


# ── lookup ──────────────────────────────────────────────────────────


async def recipe_for_url(
    session: AsyncSession, domain: str, url: str
) -> tuple[SiteProfile, ExtractionRecipe] | None:
    """Pick the active blueprint for a URL. Among a profile's active recipes,
    prefer the one whose url_pattern matches (most specific = longest pattern
    wins); a pattern-less active recipe is the fallback. Returns None if the
    domain is unknown or has no usable blueprint."""
    profile = (await session.execute(
        select(SiteProfile).where(SiteProfile.domain == domain)
    )).scalars().first()
    if profile is None:
        return None
    active = [r for r in profile.recipes if r.active]
    if not active:
        return None
    # Patterned matches, longest pattern first.
    matched = [r for r in active if _url_matches(r.url_pattern, url)]
    if matched:
        matched.sort(key=lambda r: len(r.url_pattern or ""), reverse=True)
        return profile, matched[0]
    # Fallback: an active recipe with no pattern.
    fallback = next((r for r in active if not r.url_pattern), None)
    if fallback is not None:
        return profile, fallback
    return None


async def record_outcome(recipe_id, *, success: bool) -> None:
    """Bump success/failure counters on a recipe after a fetch attempt."""
    async with SessionLocal() as session:
        async with session.begin():
            recipe = await session.get(ExtractionRecipe, recipe_id)
            if recipe is None:
                return
            if success:
                recipe.success_count += 1
            else:
                recipe.failure_count += 1


# ── seed ────────────────────────────────────────────────────────────


# A hand-written starter recipe for Reverb. JSON-LD is the most likely place
# Reverb exposes listing data in the server-rendered HTML. This is a STARTING
# POINT — use Scout's "Test fetch" to validate it against a real listing and
# edit the field map if the paths differ.
_REVERB_FIELD_MAP = {
    "title": "name",
    "description": "description",
    "price": "offers.price",
    "currency": "offers.priceCurrency",
    "condition": "offers.itemCondition",
    "url": "offers.url",
}


async def seed_reverb_profile() -> None:
    """Create the Reverb starter profile + recipe if no profile exists yet.
    Idempotent — safe to call on every boot."""
    async with SessionLocal() as session:
        async with session.begin():
            existing = (await session.execute(
                select(SiteProfile).where(SiteProfile.domain == "reverb.com")
            )).scalars().first()
            if existing is not None:
                return
            profile = SiteProfile(
                domain="reverb.com",
                display_name="Reverb",
                status=SiteProfileStatus.ACTIVE,
                notes=(
                    "Starter recipe — JSON-LD Product/Offer. Validate with "
                    "Test fetch on a real listing and adjust the field map if "
                    "the paths differ."
                ),
            )
            session.add(profile)
            await session.flush()
            session.add(ExtractionRecipe(
                site_profile_id=profile.id,
                version=1,
                strategy=ExtractionStrategy.JSONLD,
                field_map=_REVERB_FIELD_MAP,
                url_pattern="/item/",
                page_type="listing",
                result_shape="single",
                search_strategy={
                    "type": "url",
                    "pattern": "https://reverb.com/marketplace?query={q}",
                },
                needs_js=False,
                confidence=0.5,
                active=True,
            ))
    log.info("scout.seeded_reverb_profile")


# ════════════════════════ learning (Phase B) ═══════════════════════
#
# Point a learning job at a sample URL. We fetch the page, hand the model the
# structured-data candidates we find (JSON-LD, embedded JSON, price meta), and
# ask it to write an extraction recipe. We then VALIDATE the proposal by
# applying it to the same page; only a recipe that actually pulls fields gets
# saved + activated as a new version. Rides the WorkbenchRequest transport
# (kind="scout_learn") so no new worker/protocol code is needed.

_LEARN_SYSTEM = """\
You configure data-extraction recipes for websites. Given the structured data
found on a sample page, output a JSON recipe describing how to extract these
fields when present: title, price, currency, condition, year, seller, url,
description. Include ONLY fields you can actually locate in the data provided.

Pick the strategy based on WHERE the data actually is in the input:
- "jsonld": ONLY if a JSON-LD object actually contains the target fields
  (e.g. a "@type":"Product" with an "offers.price"). field_map values are
  dotted paths into that object. A JSON-LD object of @type "WebSite" or
  "Organization" is site chrome — it does NOT contain a listing price, so do
  NOT pick jsonld just because some JSON-LD exists.
- "embedded_json": the data is in an embedded JSON blob (e.g. __NEXT_DATA__);
  field_map values are dotted paths into it.
- "selectors": use this when the price/title live in plain DOM elements (no
  Product JSON-LD). Take selectors from the "PRICE-LIKE ELEMENTS" section
  provided — each line is `css_selector :: text`. Choose the selector whose
  text is the page's MAIN listing price; prefer a specific price-block
  selector (e.g. div.rc-price-block__price) over a generic one that repeats
  across many listings (e.g. a bare span.price-display). Map title from the
  page's main heading selector.

  Selector suffix syntax — these are the ONLY two forms accepted:
    - `<css>::text`             → take the element's text content
    - `<css>::attr(name)`       → take the value of an attribute
  No spaces around `::`. Do NOT write `:: text`, `@attr['x']`, `:attr(x)`,
  or anything else. Examples:
    "title":  "h1.listing-title::text"
    "url":    "meta[property='og:url']::attr(content)"
    "price":  "div.rc-price-block__price::text"
  Plain CSS without a suffix also works and takes the element's text.

Decision rule: if no JSON-LD object holds the price, you MUST use "selectors"
(or "embedded_json" if the value is in the blob) — never fall back to a
jsonld recipe whose paths don't exist.

Output ONLY a single JSON object in a ```json code block:

```json
{"strategy": "jsonld",
 "field_map": {"title": "name", "price": "offers.price", "currency": "offers.priceCurrency"},
 "needs_js": false}
```

Map a field only if you can see where its value lives in the provided data.
"""


_LEARN_SYSTEM_LIST = """\
You configure a LIST extraction recipe for a page that shows MANY items (a
search-results or price-comparison page). Output a JSON recipe that extracts
EVERY item as a row.

Steps:
- Choose `item_selector` from "REPEATING CONTAINERS" — the CSS selector
  matching each item/row. Its count should be close to the number of listings
  on the page (not 1, and not hundreds).
- Map per-item fields using the "ROW ANATOMY" section, which shows the
  descendants of one row. Use those selectors VERBATIM — DO NOT change tag
  names (if anatomy shows `div.foo`, use `div.foo`, not `span.foo`). Do not
  invent selectors that don't appear in anatomy.
- Always capture `price`; also `title`, `condition`, `url` where shown.
- Selector suffix syntax — ONLY these two forms are accepted, with NO spaces
  around `::`:
    - `<css>::text`             → element text
    - `<css>::attr(name)`       → attribute value
  Do NOT write `:: text`, `@attr['x']`, `:attr(x)`, etc. Plain CSS with no
  suffix also works and takes the element's text.

Output ONLY a single JSON object in a ```json code block:

```json
{"result_shape": "list",
 "item_selector": "div.listing-row",
 "field_map": {"title": "a.listing-title::text", "price": "span.price-display::text", "url": "a.listing-title::attr(href)"},
 "needs_js": true}
```

Pick item_selector and field selectors only from what you can see in the data.
"""


def extract_candidate_signal(html: str) -> str:
    """Reduce a page to the structured-data candidates a model needs to write
    a recipe: JSON-LD objects, an embedded-JSON blob's shape, and price/OG
    meta tags. Bounded so it fits a local model's context."""
    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []

    if soup.title and soup.title.string:
        parts.append(f"PAGE TITLE: {soup.title.string.strip()}")

    ld = list(_iter_jsonld_objects(soup))
    if ld:
        parts.append("JSON-LD OBJECTS (each is a candidate for strategy 'jsonld'):")
        for obj in ld[:6]:
            parts.append(json.dumps(obj)[:2000])

    blob = _find_embedded_json(soup)
    if blob is not None:
        parts.append(
            "EMBEDDED JSON top-level keys: " + ", ".join(list(blob.keys())[:40])
        )
        parts.append("EMBEDDED JSON sample (truncated): " + json.dumps(blob)[:3000])

    metas: list[str] = []
    for m in soup.find_all("meta"):
        key = m.get("property") or m.get("name") or ""
        val = (m.get("content") or "").strip()
        if val and ("price" in key.lower() or key.lower() in ("og:title", "description")):
            metas.append(f"{key}: {val}")
    if metas:
        parts.append("META TAGS:\n" + "\n".join(metas[:20]))

    # Price-bearing DOM elements. Many sites (Reverb) render the price into
    # plain elements, not structured data — so scan the rendered DOM for
    # currency-looking text and hand the model a selector hint for each. This
    # is what makes a 'selectors' recipe possible on JS-rendered pages.
    body = BeautifulSoup(html, "html.parser")
    for t in body(["script", "style", "noscript"]):
        t.decompose()
    price_re = re.compile(r"(?:[$£€]|USD|EUR|GBP)\s?\d[\d,]*(?:\.\d{1,2})?")
    seen: set[str] = set()
    price_hits: list[str] = []
    for node in body.find_all(string=price_re):
        parent = node.parent
        hint = _css_hint(parent)
        text = node.strip()
        key = f"{hint}::{text}"
        if not hint or key in seen:
            continue
        seen.add(key)
        price_hits.append(f"{hint} :: {text}")
        if len(price_hits) >= 15:
            break
    if price_hits:
        parts.append(
            "PRICE-LIKE ELEMENTS (css_selector :: text) — for a 'selectors' "
            "recipe, use the selector whose text is the actual listing price:\n"
            + "\n".join(price_hits)
        )

    # Repeating containers: ancestor selectors of price nodes that recur many
    # times — candidate item_selector values for a 'list' recipe. The one whose
    # count ≈ number of listings is usually the row container.
    container_counts: Counter = Counter()
    for node in body.find_all(string=price_re):
        el = node.parent
        depth = 0
        while el is not None and depth < 6:
            classes = [c for c in (el.get("class") or []) if c][:2]
            if classes:
                container_counts[el.name + "".join(f".{c}" for c in classes)] += 1
            el = el.parent
            depth += 1
    repeating = [(sel, n) for sel, n in container_counts.most_common(12) if n >= 3]
    if repeating:
        parts.append(
            "REPEATING CONTAINERS (css_selector :: count) — for a 'list' recipe, "
            "pick item_selector whose count ≈ the number of listings on the page:\n"
            + "\n".join(f"{sel} :: {n}" for sel, n in repeating)
        )

        # ROW ANATOMY: the *inside* of a single repeating container, so the
        # model can write accurate relative selectors for title/price/url
        # instead of inventing tag names it never saw. Pick the highest-count
        # candidate whose sample has substantive structure — the topmost
        # repeating selector is often the price LEAF (no children), not the
        # row.
        chosen_sel = None
        sample = None
        for sel, _count in repeating[:8]:
            try:
                s = body.select_one(sel)
            except Exception:
                s = None
            if s is None:
                continue
            classed_descendants = sum(
                1 for el in s.find_all(True) if (el.get("class") or [])
            )
            if classed_descendants >= 4:
                chosen_sel, sample = sel, s
                break
        if sample is None and repeating:
            chosen_sel = repeating[0][0]
            try:
                sample = body.select_one(chosen_sel)
            except Exception:
                sample = None
        top_sel = chosen_sel
        if sample is not None:
            anatomy: list[str] = []
            for el in sample.find_all(True):
                classes = [c for c in (el.get("class") or []) if c][:3]
                if not classes:
                    continue
                sel = el.name + "".join(f".{c}" for c in classes)
                text = " ".join(el.get_text(" ", strip=True).split())[:80]
                href = el.get("href")
                if text:
                    anatomy.append(f"{sel}::text  →  {text}")
                elif href:
                    anatomy.append(f"{sel}::attr(href)  →  {str(href)[:80]}")
                if len(anatomy) >= 35:
                    break
            if anatomy:
                parts.append(
                    f"ROW ANATOMY for one '{top_sel}' (these are RELATIVE "
                    f"selectors inside item_selector — use them verbatim, "
                    f"matching tag names EXACTLY):\n" + "\n".join(anatomy)
                )

    visible = " ".join(body.get_text(" ").split())
    if visible:
        parts.append("VISIBLE TEXT SAMPLE:\n" + visible[:2500])

    return "\n\n".join(parts)[:14000] or "(no structured data found on the page)"


def build_learn_messages(
    domain: str, signal: str, result_shape: str = "single"
) -> list[dict[str, str]]:
    system = _LEARN_SYSTEM_LIST if result_shape == "list" else _LEARN_SYSTEM
    shape_note = (
        "This page is a LIST page (many items) — produce a list recipe."
        if result_shape == "list"
        else "This page is a SINGLE-item page — produce a single-record recipe."
    )
    user = (
        f"## Site\n{domain}\n\n"
        f"## Page kind\n{shape_note}\n\n"
        f"## Structured data found on the sample page\n{signal}\n\n"
        f"## Task\nProduce the extraction recipe JSON per the rules. "
        f"Output only the JSON object."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _parse_recipe_json(text: str) -> dict | None:
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue
    return None


async def _pick_learn_worker():
    from app.orchestrator.registry import registry
    for pool in _LEARN_POOLS:
        workers = await registry.by_pool(pool)
        if not workers:
            continue
        idle = [w for w in workers if w.current_task_id is None]
        return (idle or workers)[0]
    return None


async def dispatch_site_learning(job_id: uuid.UUID) -> str | None:
    """Fetch the sample page, hand its structured-data candidates to a worker,
    and ask for a recipe. Returns None on success or an error string."""
    from app.orchestrator import web_fetch
    from app.orchestrator.protocol import WorkbenchRequestMsg

    async with SessionLocal() as session:
        job = await session.get(SiteLearningJob, job_id)
        if not job:
            return "learning job not found"
        profile = await session.get(SiteProfile, job.site_profile_id)
        domain = profile.domain if profile else ""
        url = job.sample_url
        use_browser = bool(job.use_browser)
        result_shape = getattr(job, "result_shape", None) or "single"

    html, error = await web_fetch.fetch_raw(url, render=use_browser)
    if html is None:
        return f"could not fetch the sample page: {error}"

    signal = extract_candidate_signal(html)
    messages = build_learn_messages(domain, signal, result_shape=result_shape)

    async with SessionLocal() as session:
        async with session.begin():
            job = await session.get(SiteLearningJob, job_id)
            if job:
                job.sample_html = html

    worker = await _pick_learn_worker()
    if worker is None:
        return (
            "No worker is online in the docs / researcher / planner / coder "
            "pools to learn the recipe."
        )

    msg = WorkbenchRequestMsg(
        job_id=job_id, kind="scout_learn", model=None, messages=messages,
    )
    try:
        await worker.send(msg.model_dump(mode="json"))
    except Exception as exc:
        log.warning("scout.learn_dispatch_failed", job=str(job_id), error=str(exc))
        return f"failed to reach worker {worker.name}: {exc}"

    async with SessionLocal() as session:
        async with session.begin():
            job = await session.get(SiteLearningJob, job_id)
            if job:
                job.status = "running"
                job.worker_name = worker.name
    log.info("scout.learn_dispatched", job=str(job_id), worker=worker.name)
    return None


async def run_learning_job(job_id: uuid.UUID) -> None:
    """Background entrypoint: run dispatch and, if it fails synchronously
    (bad fetch / no worker / render error), record the error on the job so it
    never strands in a running state. Designed to be fired via
    asyncio.create_task from the route so a slow render doesn't block the POST."""
    try:
        err = await dispatch_site_learning(job_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("scout.learn_job_crashed", job=str(job_id))
        err = str(exc)
    if err:
        async with SessionLocal() as session:
            async with session.begin():
                job = await session.get(SiteLearningJob, job_id)
                if job and job.status not in ("done",):
                    job.status = "error"
                    job.error = err


async def handle_site_learning_result(
    job_id: uuid.UUID, success: bool, content: str, error: str | None
) -> None:
    """Validate the model's proposed recipe against the stashed page and, if
    it extracts anything, save + activate it as a new recipe version."""
    async with SessionLocal() as session:
        async with session.begin():
            job = await session.get(SiteLearningJob, job_id)
            if not job:
                log.warning("scout.learn_result_no_job", job=str(job_id))
                return
            if not success:
                job.status = "error"
                job.error = error or "worker reported failure"
                return

            proposed = _parse_recipe_json(content or "")
            if not proposed:
                job.status = "error"
                job.error = "could not parse a recipe JSON from the response"
                job.result = {"raw": (content or "")[:4000]}
                return

            shape = str(
                proposed.get("result_shape") or getattr(job, "result_shape", None) or "single"
            )
            field_map = proposed.get("field_map")
            if not isinstance(field_map, dict) or not field_map:
                job.status = "error"
                job.error = "proposed recipe has no usable field_map"
                job.result = {"proposed": proposed}
                return

            list_item_selector = None
            if shape == "list":
                # List recipes are selector-based over a repeating row.
                strategy = ExtractionStrategy.SELECTORS
                list_item_selector = proposed.get("item_selector")
                if not list_item_selector:
                    job.status = "error"
                    job.error = "list recipe is missing item_selector"
                    job.result = {"proposed": proposed}
                    return
            else:
                try:
                    strategy = ExtractionStrategy(str(proposed.get("strategy") or "jsonld"))
                except ValueError:
                    strategy = ExtractionStrategy.JSONLD

            # Validate against the exact page the model analyzed.
            transient = SimpleNamespace(
                strategy=strategy, field_map=field_map,
                result_shape=shape, list_item_selector=list_item_selector,
            )
            fields = apply_recipe(job.sample_html or "", transient)
            if not recipe_has_data(fields):
                job.status = "error"
                job.error = "the proposed recipe matched nothing on the sample page"
                job.result = {
                    "proposed": proposed,
                    "candidate_signal": extract_candidate_signal(job.sample_html or ""),
                }
                return

            url_pattern = getattr(job, "url_pattern", None) or None
            page_type = getattr(job, "page_type", None) or "listing"

            # Save as a new blueprint version. Deactivate only blueprints with
            # the SAME url_pattern (re-learning that page type) so different
            # page types coexist as separate active blueprints.
            recipes = (await session.execute(
                select(ExtractionRecipe).where(
                    ExtractionRecipe.site_profile_id == job.site_profile_id
                )
            )).scalars().all()
            next_version = max((r.version for r in recipes), default=0) + 1
            for r in recipes:
                if (r.url_pattern or None) == url_pattern:
                    r.active = False
            needs_js = bool(job.use_browser) or bool(proposed.get("needs_js"))
            if shape == "list":
                n_found = len(fields.get("items") or [])
                confidence = min(1.0, n_found / 5.0)
                fields_found = [f"{n_found} items"]
            else:
                confidence = min(1.0, len(fields) / 6.0)
                fields_found = list(fields.keys())
            session.add(ExtractionRecipe(
                site_profile_id=job.site_profile_id,
                version=next_version,
                strategy=strategy,
                field_map=field_map,
                url_pattern=url_pattern,
                page_type=page_type,
                result_shape=shape,
                list_item_selector=list_item_selector,
                needs_js=needs_js,
                confidence=confidence,
                active=True,
                last_validated_at=datetime.now(timezone.utc),
            ))
            profile = await session.get(SiteProfile, job.site_profile_id)
            if profile:
                profile.status = SiteProfileStatus.ACTIVE
            job.status = "done"
            job.result = {
                "version": next_version,
                "strategy": strategy.value,
                "result_shape": shape,
                "fields_found": fields_found,
                "sample": fields,
            }
    log.info("scout.learn_result_applied", job=str(job_id))
