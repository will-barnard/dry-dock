"""FastAPI entrypoint for the dry-dock orchestrator."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from app.auth import AuthRedirect, get_current_user
from app.config import get_settings
from app.db import Base, engine
from app.orchestrator.dispatcher import dispatcher
from app.routes import auth, dashboard, projects, streams, tasks, workers


def _configure_logging() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )


_configure_logging()
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Light bootstrap — create tables if they don't exist. For real schema
    # evolution we rely on Alembic; this just gives a frictionless first boot.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await dispatcher.start()
    log.info("orchestrator.started")
    try:
        yield
    finally:
        await dispatcher.stop()
        await engine.dispose()
        log.info("orchestrator.stopped")


settings = get_settings()
app = FastAPI(title="dry-dock orchestrator", version="0.1.0", lifespan=lifespan)

# Session cookie middleware. Must be added before any router that calls
# request.session. `https_only=True` requires the request to arrive over HTTPS
# at the proxy boundary — Beachhead's nginx-proxy terminates TLS so that's the
# common case.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="drydock_session",
    https_only=settings.session_https_only,
    same_site="lax",
    max_age=60 * 60 * 24 * 30,  # 30 days
)


# Redirect-based auth: routes that depend on get_current_user raise
# AuthRedirect for unauthenticated visitors. Convert to a 303.
@app.exception_handler(AuthRedirect)
async def _auth_redirect_handler(request: Request, exc: AuthRedirect) -> RedirectResponse:
    return RedirectResponse(exc.location, status_code=303)


# ── Public endpoints (no auth required) ─────────────────────────────


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


app.include_router(auth.router)  # /setup, /login, /logout

# Worker WebSocket auths with a shared secret, NOT the user session. Keep it
# outside the session-gated set.
app.include_router(workers.router)  # /ws/worker


# ── Authed endpoints ────────────────────────────────────────────────
# `dependencies=[Depends(get_current_user)]` applies the auth gate to every
# route in the included router. FastAPI caches dependency results per request,
# so route handlers can declare the user explicitly without an extra DB hit.

_auth = [Depends(get_current_user)]

# JSON API
app.include_router(projects.router, dependencies=_auth)
app.include_router(tasks.router, dependencies=_auth)
app.include_router(workers.http_router, dependencies=_auth)
app.include_router(streams.router, dependencies=_auth)

# HTMX server-rendered views
app.include_router(dashboard.router, dependencies=_auth)
