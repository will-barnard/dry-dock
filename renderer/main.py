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
    wait_until: str = Query("networkidle"),
    timeout_ms: int = Query(20000),
    settle_ms: int = Query(1500),
):
    """Render a URL and return the post-JavaScript HTML.

    `wait_until=networkidle` waits for the page to stop making requests, which
    is usually when client-rendered data has landed; `settle_ms` adds a small
    grace period for late XHR. Errors return 502 with a JSON message so the
    backend can degrade gracefully.
    """
    if _browser is None:
        return JSONResponse({"error": "browser not ready"}, status_code=503)
    context = await _browser.new_context(user_agent=_USER_AGENT)
    page = await context.new_page()
    try:
        await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        if settle_ms > 0:
            await page.wait_for_timeout(settle_ms)
        html = await page.content()
        return PlainTextResponse(html)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)
    finally:
        await context.close()
