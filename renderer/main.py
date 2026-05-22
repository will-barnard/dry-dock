"""dry-dock headless renderer.

A tiny single-purpose service: given a URL, load it in a real (headless)
Chromium, wait for the page's JavaScript to populate content, and return the
rendered HTML. This is how Scout reads client-rendered sites (Reverb et al.)
whose listing data isn't in the static response.

Isolated in its own container so the ~500MB Chromium dependency stays out of
the orchestrator image. The backend calls GET /render?url=… on the internal
network; it is never exposed publicly.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

app = FastAPI(title="dry-dock renderer")

_USER_AGENT = os.environ.get(
    "RENDERER_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)

# One long-lived browser; a fresh context per request for isolation.
_pw = None
_browser = None


@app.on_event("startup")
async def _startup() -> None:
    global _pw, _browser
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _browser is not None:
        await _browser.close()
    if _pw is not None:
        await _pw.stop()


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "browser": _browser is not None}


@app.get("/render", response_class=PlainTextResponse)
async def render(
    url: str = Query(...),
    timeout_ms: int = Query(25000),
    settle_ms: int = Query(2500),
):
    """Render a URL and return the post-JavaScript HTML.

    Resilient by design: many SPAs (Reverb included) fire continuous
    background requests so the network never goes 'idle' — waiting for that
    would always time out. Instead we navigate on `domcontentloaded`, give
    the network a best-effort window to settle so client-rendered data lands,
    add a short grace period, and return whatever rendered. We only 502 on a
    real navigation failure (DNS, connection refused, browser crash), never on
    a slow page.
    """
    if _browser is None:
        return JSONResponse({"error": "browser not ready"}, status_code=503)
    context = await _browser.new_context(user_agent=_USER_AGENT)
    page = await context.new_page()
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            pass  # navigation slow — use whatever loaded
        # Best-effort wait for client-rendered data; don't fail if it never settles.
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            pass
        if settle_ms > 0:
            await page.wait_for_timeout(settle_ms)
        html = await page.content()
        return PlainTextResponse(html)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)
    finally:
        await context.close()
