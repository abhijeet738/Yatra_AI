# app/main.py
"""FastAPI entry point: app creation, startup/shutdown, and router wiring."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.routes import health
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Code before `yield` runs once at startup; code after it runs once at shutdown."""
    settings = get_settings()
    configure_logging(env=settings.env)  # must run before anything else logs

    # One shared HTTP client for all outbound calls: connection pooling + a default timeout
    app.state.http = httpx.AsyncClient(timeout=10)

    # Dependency checks that /ready runs. Empty for now; the cache (week 2) and the
    # database (week 4) will register an async ping function here under their own name.
    app.state.readiness_checks = {}

    log.info("startup_complete", env=settings.env)
    yield  # the app serves requests while paused here

    await app.state.http.aclose()  # close pooled connections cleanly on shutdown
    log.info("shutdown_complete")


# `lifespan` hooks the startup/shutdown code above into the app
app = FastAPI(title="YatraAI", version="1.0.0", lifespan=lifespan)

app.include_router(health.router)  # adds GET /health and GET /ready
# The trips router joins in week 5, once the agent graph exists.