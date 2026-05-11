"""FastAPI entrypoint for the dry-dock orchestrator."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.config import get_settings
from app.db import Base, engine
from app.orchestrator.dispatcher import dispatcher
from app.routes import dashboard, projects, streams, tasks, workers


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


app = FastAPI(title="dry-dock orchestrator", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


# JSON API
app.include_router(projects.router)
app.include_router(tasks.router)
app.include_router(workers.http_router)
app.include_router(streams.router)

# Worker WebSocket lives under /ws/* — frontend nginx proxies this with upgrade.
app.include_router(workers.router)

# HTMX server-rendered views
app.include_router(dashboard.router)
