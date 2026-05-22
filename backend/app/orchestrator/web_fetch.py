"""URL fetch tool for the Operator's agentic mode.

Lightweight by design: an httpx GET, then we pull the useful signal out of
the HTML — JSON-LD blocks, OpenGraph / product meta tags, and the visible
text — and hand the model a compact text summary. No headless browser, so
heavily client-rendered pages (Reverb listing pages, for instance) may not
expose live data; when that happens the structured-data extraction is the
best shot, since many such sites still server-render a JSON-LD <script>.

Good-citizen behaviour: respects robots.txt, sends a real User-Agent,
caps response size, and rate-limits per host.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser

import httpx
import structlog
from bs4 import BeautifulSoup

from app.db import SessionLocal

log = structlog.get_logger()

USER_AGENT = "dry-dock-operator/0.1 (+https://github.com/; research assistant)"

# Caps to keep one fetch bounded.
_MAX_BYTES = 2_000_000          # don't slurp giant pages
_MAX_TEXT_CHARS = 8_000         # trim extracted text fed to the model
_FETCH_TIMEOUT = 12.0
_MIN_SECONDS_BETWEEN_HOST_HITS = 1.0

# Per-host last-hit timestamps for naive rate limiting.
_last_hit: dict[str, float] = defaultdict(float)
_host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# robots.txt parser cache, keyed by scheme+host.
_robots_cache: dict[str, RobotFileParser | None] = {}


class FetchError(Exception):
    pass


async def _robots_allows(client: httpx.AsyncClient, url: str) -> bool:
    """Best-effort robots.txt check. On any error we *allow* — robots is
    advisory and a fetch failure shouldn't hard-block a single user-driven
    lookup, but we honour an explicit Disallow when we can read one."""
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    if root not in _robots_cache:
        rp: RobotFileParser | None = RobotFileParser()
        try:
            resp = await client.get(urljoin(root, "/robots.txt"))
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp = None  # no robots → unrestricted
        except Exception:
            rp = None
        _robots_cache[root] = rp
    rp = _robots_cache[root]
    if rp is None:
        return True
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


async def _rate_limit(host: str) -> None:
    async with _host_locks[host]:
        elapsed = time.monotonic() - _last_hit[host]
        if elapsed < _MIN_SECONDS_BETWEEN_HOST_HITS:
            await asyncio.sleep(_MIN_SECONDS_BETWEEN_HOST_HITS - elapsed)
        _last_hit[host] = time.monotonic()


def _extract(html: str, url: str) -> str:
    """Pull a compact, model-friendly summary out of an HTML page:
    title, meta description, OpenGraph/price meta, JSON-LD blocks, and the
    leading visible text. Structured data first because that's where prices
    and product facts live on marketplace pages."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []

    if soup.title and soup.title.string:
        out.append(f"TITLE: {soup.title.string.strip()}")

    # Meta description + OpenGraph / product price tags.
    meta_bits: list[str] = []
    for m in soup.find_all("meta"):
        key = m.get("property") or m.get("name") or ""
        val = (m.get("content") or "").strip()
        if not val:
            continue
        key_l = key.lower()
        if key_l in (
            "description", "og:title", "og:description",
            "og:price:amount", "og:price:currency",
            "product:price:amount", "product:price:currency",
            "twitter:data1", "twitter:label1",
        ):
            meta_bits.append(f"{key}: {val}")
    if meta_bits:
        out.append("META:\n" + "\n".join(meta_bits))

    # JSON-LD — often holds Product/Offer/price on marketplace + e-commerce.
    ld_blocks: list[str] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or "").strip()
        if raw:
            ld_blocks.append(raw[:2000])
    if ld_blocks:
        out.append("STRUCTURED DATA (JSON-LD):\n" + "\n---\n".join(ld_blocks))

    # Visible text — strip scripts/styles/nav, collapse whitespace.
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    if text:
        out.append("PAGE TEXT:\n" + text[:_MAX_TEXT_CHARS])

    summary = "\n\n".join(out).strip()
    return summary or "(the page returned no extractable text or structured data)"


async def fetch(url: str) -> str:
    """Fetch a URL and return a compact text summary, or a human-readable
    error string (never raises — the tool layer wants a string either way)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"Refused to fetch '{url}': only http(s) URLs are supported."

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT, follow_redirects=True, headers=headers
        ) as client:
            if not await _robots_allows(client, url):
                return f"robots.txt disallows fetching {url}."
            await _rate_limit(parsed.netloc)
            resp = await client.get(url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "xml" not in ctype and "text" not in ctype:
                return (
                    f"Fetched {url} but its content-type is '{ctype}', not a "
                    f"readable web page. Skipping."
                )
            body = resp.text[:_MAX_BYTES]
    except httpx.HTTPStatusError as exc:
        return f"Fetch of {url} failed: HTTP {exc.response.status_code}."
    except httpx.HTTPError as exc:
        return f"Fetch of {url} failed: {exc}."
    except Exception as exc:  # noqa: BLE001
        log.exception("web_fetch.unexpected", url=url[:200])
        return f"Fetch of {url} failed: {exc}."

    # Site-aware extraction (Scout): if this domain has an active recipe, try
    # it first for clean structured fields. Fall back to generic extraction
    # when there's no recipe or it matched nothing.
    from app.orchestrator import scout

    domain = scout.domain_of(url)
    if domain:
        try:
            async with SessionLocal() as session:
                hit = await scout.active_recipe_for_domain(session, domain)
            if hit is not None:
                profile, recipe = hit
                fields = scout.apply_recipe(body, recipe)
                if fields:
                    await scout.record_outcome(recipe.id, success=True)
                    structured = scout.format_fields(domain, url, fields)
                    # Append a trimmed generic body too, so the model has
                    # surrounding context beyond the mapped fields.
                    return f"{structured}\n\n---\n{_extract(body, url)[:2000]}"
                else:
                    await scout.record_outcome(recipe.id, success=False)
                    log.info("web_fetch.recipe_miss", domain=domain)
        except Exception:
            log.exception("web_fetch.scout_failed", domain=domain)

    extracted = _extract(body, url)
    return f"Fetched: {url}\n\n{extracted}"
